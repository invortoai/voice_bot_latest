"""Integration tests for Twilio webhook endpoints.

Covers:
- POST /twilio/incoming  — returns TwiML XML; assigns worker; creates call record
- POST /twilio/status    — updates call status; releases worker on terminal states

NOTE: Twilio sends form-encoded POST bodies, not JSON.
"""

import pytest
from unittest.mock import patch, AsyncMock


# ---------------------------------------------------------------------------
# Auth bypass — Twilio signature validation is enforced everywhere now.
# We can't generate real Twilio HMAC signatures in tests, so we patch the
# validator. Auth logic is tested separately in test_security_*.py files.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_twilio_auth():
    """Bypass Twilio auth for integration tests.

    Can't generate real Twilio HMAC signatures in tests. Auth logic is
    tested separately in test_security_*.py and test_security_fail_closed.py.
    """
    with patch("app.routes.twilio._verify_twilio_webhook", new_callable=AsyncMock):
        yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ASSISTANT_PAYLOAD = {
    "name": "Twilio Test Bot",
    "system_prompt": "You are a helpful assistant.",
}

PHONE_PAYLOAD = {
    "phone_number": "+15005550006",
    "provider": "twilio",
    "provider_credentials": {"account_sid": "AC123", "auth_token": "token123"},
    "is_inbound_enabled": True,
}


@pytest.fixture
async def assistant(runner_client):
    resp = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
    return resp.json()


@pytest.fixture
async def phone_number(runner_client, assistant):
    payload = {**PHONE_PAYLOAD, "assistant_id": assistant["id"]}
    resp = await runner_client.post("/phone-numbers", json=payload)
    return resp.json()


def _incoming_form(call_sid="CA-TW-001", from_="+14155551234", to="+15005550006"):
    """Build form data matching what Twilio sends to /twilio/incoming."""
    return {"CallSid": call_sid, "From": from_, "To": to}


def _status_form(call_sid="CA-TW-001", call_status="completed", duration="120"):
    """Build form data matching what Twilio sends to /twilio/status."""
    return {"CallSid": call_sid, "CallStatus": call_status, "CallDuration": duration}


# ---------------------------------------------------------------------------
# /twilio/incoming tests
# ---------------------------------------------------------------------------


class TestTwilioIncomingWebhook:
    async def test_with_available_worker_returns_xml(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/twilio/incoming",
            data=_incoming_form(to="+15005550006"),
        )
        assert resp.status_code == 200
        assert "xml" in resp.headers["content-type"].lower()

    async def test_response_contains_stream_verb(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/twilio/incoming",
            data=_incoming_form(to="+15005550006"),
        )
        assert b"<Stream" in resp.content

    async def test_response_contains_connect_verb(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/twilio/incoming",
            data=_incoming_form(to="+15005550006"),
        )
        assert b"<Connect>" in resp.content or b"<Connect" in resp.content

    async def test_response_stream_url_contains_ws(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/twilio/incoming",
            data=_incoming_form(to="+15005550006"),
        )
        assert b"ws" in resp.content.lower()

    async def test_worker_gets_assigned_on_incoming(
        self, runner_client, phone_number, worker_in_pool
    ):
        await runner_client.post(
            "/twilio/incoming",
            data=_incoming_form(call_sid="CA-ASSIGN-01", to="+15005550006"),
        )
        assert worker_in_pool.current_call_sid == "CA-ASSIGN-01"
        assert not worker_in_pool.is_accepting_calls

    async def test_no_available_worker_returns_xml_with_say(
        self, runner_client, phone_number
    ):
        """When no worker is free, response should be TwiML saying agents are busy."""
        resp = await runner_client.post(
            "/twilio/incoming",
            data=_incoming_form(to="+15005550006"),
        )
        assert resp.status_code == 200
        assert "xml" in resp.headers["content-type"].lower()
        # Response should mention busy/agents instead of Stream
        assert b"<Stream" not in resp.content
        assert b"Say" in resp.content or b"say" in resp.content

    async def test_creates_call_record_in_db(
        self, runner_client, phone_number, worker_in_pool
    ):
        await runner_client.post(
            "/twilio/incoming",
            data=_incoming_form(call_sid="CA-DB-CREATE", to="+15005550006"),
        )
        calls_resp = await runner_client.get("/calls")
        calls = calls_resp.json()["calls"]
        assert len(calls) == 1
        assert calls[0]["call_sid"] == "CA-DB-CREATE"
        assert calls[0]["direction"] == "inbound"
        assert calls[0]["provider"] == "twilio"

    async def test_call_record_stores_from_and_to(
        self, runner_client, phone_number, worker_in_pool
    ):
        await runner_client.post(
            "/twilio/incoming",
            data=_incoming_form(
                call_sid="CA-NUMBERS",
                from_="+14155551111",
                to="+15005550006",
            ),
        )
        calls_resp = await runner_client.get("/calls")
        call = calls_resp.json()["calls"][0]
        # GET /calls returns masked phone numbers (DAAI-146 PII masking)
        assert call["from_number"] == "***1111"
        assert call["to_number"] == "***0006"

    async def test_response_is_valid_xml(
        self, runner_client, phone_number, worker_in_pool
    ):
        import xml.etree.ElementTree as ET

        resp = await runner_client.post(
            "/twilio/incoming",
            data=_incoming_form(to="+15005550006"),
        )
        root = ET.fromstring(resp.content)
        assert root.tag == "Response"

    async def test_response_includes_pause_with_max_duration(
        self, runner_client, phone_number, worker_in_pool
    ):
        import xml.etree.ElementTree as ET

        resp = await runner_client.post(
            "/twilio/incoming",
            data=_incoming_form(to="+15005550006"),
        )
        root = ET.fromstring(resp.content)
        pause = root.find("Pause")
        assert pause is not None
        assert int(pause.get("length", 0)) > 0


# ---------------------------------------------------------------------------
# /twilio/status tests
# ---------------------------------------------------------------------------


class TestTwilioStatusWebhook:
    async def test_status_returns_ok(self, runner_client):
        resp = await runner_client.post(
            "/twilio/status",
            data=_status_form(call_sid="CA-STAT-001", call_status="completed"),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_completed_status_updates_db(
        self, runner_client, phone_number, worker_in_pool
    ):
        # Create call first
        await runner_client.post(
            "/twilio/incoming",
            data=_incoming_form(call_sid="CA-STAT-COMP", to="+15005550006"),
        )
        # Update via status webhook
        await runner_client.post(
            "/twilio/status",
            data=_status_form(
                call_sid="CA-STAT-COMP", call_status="completed", duration="90"
            ),
        )
        calls_resp = await runner_client.get("/calls")
        call = calls_resp.json()["calls"][0]
        assert call["status"] == "completed"
        assert call["duration_seconds"] == 90

    async def test_completed_releases_worker(
        self, runner_client, phone_number, worker_in_pool
    ):
        # Assign worker via incoming
        await runner_client.post(
            "/twilio/incoming",
            data=_incoming_form(call_sid="CA-RELEASE", to="+15005550006"),
        )
        assert not worker_in_pool.is_accepting_calls

        # Status completed → should release
        await runner_client.post(
            "/twilio/status",
            data=_status_form(call_sid="CA-RELEASE", call_status="completed"),
        )
        assert worker_in_pool.is_accepting_calls
        assert worker_in_pool.current_call_sid is None

    @pytest.mark.parametrize(
        "terminal_status", ["failed", "busy", "no-answer", "canceled"]
    )
    async def test_terminal_statuses_release_worker(
        self, runner_client, terminal_status, worker_in_pool
    ):
        from app.services import call_service

        call_service.create(
            call_sid=f"CA-TERM-{terminal_status}",
            direction="inbound",
            from_number="+1111",
            to_number="+2222",
            provider="twilio",
        )
        # Manually assign call to worker
        worker_in_pool.current_call_sid = f"CA-TERM-{terminal_status}"

        await runner_client.post(
            "/twilio/status",
            data={
                "CallSid": f"CA-TERM-{terminal_status}",
                "CallStatus": terminal_status,
            },
        )
        assert worker_in_pool.is_accepting_calls

    async def test_non_terminal_status_does_not_release_worker(
        self, runner_client, phone_number, worker_in_pool
    ):
        from app.services import call_service

        call_service.create(
            call_sid="CA-INPROG",
            direction="inbound",
            from_number="+1111",
            to_number="+2222",
            provider="twilio",
        )
        worker_in_pool.current_call_sid = "CA-INPROG"

        await runner_client.post(
            "/twilio/status",
            data={"CallSid": "CA-INPROG", "CallStatus": "in-progress"},
        )
        # Worker should still be assigned
        assert not worker_in_pool.is_accepting_calls
        assert worker_in_pool.current_call_sid == "CA-INPROG"

    async def test_status_without_duration_does_not_raise(self, runner_client):
        resp = await runner_client.post(
            "/twilio/status",
            data={"CallSid": "CA-NO-DUR", "CallStatus": "completed"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /twilio/status → sync_call_request_outcome tests
# ---------------------------------------------------------------------------


def _seed_call_request_for_sync(pg_container, org_id, phone_num_digits="5005550006"):
    """Create a call_request row via service layer (satisfies bot_id NOT NULL)."""
    import psycopg2
    from app.services import assistant_service, phone_number_service
    from app.services import call_request as cr_svc

    to_number = f"+1{phone_num_digits}"
    asst = assistant_service.create(
        name=f"Sync Bot {phone_num_digits}", system_prompt="t", org_id=org_id
    )
    phone = phone_number_service.create(
        phone_number=to_number,
        org_id=org_id,
        provider="twilio",
        provider_credentials={"account_sid": "AC123", "auth_token": "tok"},
    )
    row = cr_svc.create(
        org_id=org_id,
        assistant_id=str(asst["id"]),
        phone_number_id=str(phone["id"]),
        to_number=to_number,
        priority=5,
    )
    # Advance to 'initiated' so lifecycle status tests are meaningful
    with psycopg2.connect(pg_container) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE call_requests SET status = 'initiated' WHERE id = %s",
                (str(row["id"]),),
            )
    return str(row["id"])


class TestTwilioStatusSync:
    """Verify that terminal status webhooks sync the outcome to call_requests."""

    async def test_terminal_status_syncs_to_call_requests(
        self, runner_client, pg_container, test_org_id
    ):
        import psycopg2
        from app.services import call_service

        call_request_id = _seed_call_request_for_sync(
            pg_container, test_org_id, "5005550006"
        )

        call_sid = "CA-SYNC-TERM"
        call_service.create(
            call_sid=call_sid,
            direction="outbound",
            from_number="+15005550006",
            to_number="+14155551234",
            org_id=test_org_id,
            provider="twilio",
            parent_call_sid=call_request_id,
        )

        await runner_client.post(
            "/twilio/status",
            data={"CallSid": call_sid, "CallStatus": "completed", "CallDuration": "90"},
        )

        with psycopg2.connect(pg_container) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, call_status, call_duration_seconds FROM call_requests WHERE id = %s",
                    (call_request_id,),
                )
                row = cur.fetchone()
        assert row[0] == "completed"
        assert row[1] == "completed"
        assert row[2] == 90

    async def test_mid_call_status_syncs_call_status_only(
        self, runner_client, pg_container, test_org_id, worker_in_pool
    ):
        import psycopg2
        from app.services import call_service

        call_request_id = _seed_call_request_for_sync(
            pg_container, test_org_id, "5005550007"
        )

        call_sid = "CA-SYNC-MID"
        call_service.create(
            call_sid=call_sid,
            direction="outbound",
            from_number="+15005550006",
            to_number="+14155551234",
            org_id=test_org_id,
            provider="twilio",
            parent_call_sid=call_request_id,
        )
        worker_in_pool.current_call_sid = call_sid

        await runner_client.post(
            "/twilio/status",
            data={"CallSid": call_sid, "CallStatus": "in-progress"},
        )

        with psycopg2.connect(pg_container) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, call_status FROM call_requests WHERE id = %s",
                    (call_request_id,),
                )
                row = cur.fetchone()
        # Lifecycle status stays unchanged; only call_status is updated
        assert row[0] == "initiated"
        assert row[1] == "in-progress"

    async def test_no_parent_call_sid_does_not_raise(self, runner_client):
        """Calls without parent_call_sid (e.g. inbound) should not cause errors."""
        resp = await runner_client.post(
            "/twilio/status",
            data={"CallSid": "CA-NO-PARENT", "CallStatus": "completed"},
        )
        assert resp.status_code == 200
