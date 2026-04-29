"""Integration tests for the /mcube/call webhook endpoint."""

import pytest


ASSISTANT_PAYLOAD = {
    "name": "MCube Test Bot",
    "system_prompt": "You are helpful.",
    "model": "gpt-4.1-nano",
}


# MCube sends full numbers (e.g. +918001234567) but the lookup uses the last
# 10 digits.  Store the phone number as the bare 10-digit form so the exact
# match in phone_number_service.get_by_number() succeeds.
PHONE_NUMBER_DIGITS = "8001234567"  # stored in DB
TO_NUMBER_FULL = "+91" + PHONE_NUMBER_DIGITS  # what MCube sends in toNumber

PHONE_PAYLOAD = {
    "phone_number": PHONE_NUMBER_DIGITS,
    "provider": "mcube",
    "provider_credentials": {"token": "mc-test-token"},
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


def _connecting_payload(call_id="CALL-001", to_number=TO_NUMBER_FULL):
    return {
        "callId": call_id,
        "callDirection": "inbound",
        "fromNumber": "+914155551234",
        "toNumber": to_number,
        "dialStatus": "CONNECTING",
    }


class TestMcubeConnectWebhook:
    async def test_connecting_with_worker_returns_wss_url(
        self, runner_client, phone_number, worker_in_pool
    ):
        payload = _connecting_payload(to_number=TO_NUMBER_FULL)
        resp = await runner_client.post("/mcube/call", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert "wss_url" in data
        assert "ws/mcube/CALL-001" in data["wss_url"]

    async def test_connecting_no_worker_returns_503(self, runner_client, phone_number):
        payload = _connecting_payload(to_number=TO_NUMBER_FULL)
        resp = await runner_client.post("/mcube/call", json=payload)
        assert resp.status_code == 503

    async def test_connecting_no_assistant_returns_404(
        self, runner_client, worker_in_pool
    ):
        """Call to a number with no assistant config → 404."""
        payload = _connecting_payload(to_number="+919999988888")  # not registered
        resp = await runner_client.post("/mcube/call", json=payload)
        assert resp.status_code == 404

    async def test_connecting_creates_call_record(
        self, runner_client, phone_number, worker_in_pool
    ):
        payload = _connecting_payload(to_number=TO_NUMBER_FULL)
        await runner_client.post("/mcube/call", json=payload)

        # Verify call record was created
        calls_resp = await runner_client.get("/calls")
        assert calls_resp.status_code == 200
        calls = calls_resp.json()["calls"]
        assert len(calls) == 1
        assert calls[0]["call_sid"] == "CALL-001"

    async def test_busy_status_updates_call_and_releases_worker(
        self, runner_client, phone_number, worker_in_pool
    ):
        # First, create a call record via CONNECTING
        conn_payload = _connecting_payload(to_number=TO_NUMBER_FULL)
        await runner_client.post("/mcube/call", json=conn_payload)

        # Now send BUSY hangup
        busy_payload = {
            "callId": "CALL-001",
            "dialStatus": "BUSY",
        }
        resp = await runner_client.post("/mcube/call", json=busy_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"

        # Worker should be released (available again)
        assert worker_in_pool.current_call_sid is None
        assert worker_in_pool.is_accepting_calls

    async def test_answer_with_endtime_completes_call(
        self, runner_client, phone_number, worker_in_pool
    ):
        conn_payload = _connecting_payload(to_number=TO_NUMBER_FULL)
        await runner_client.post("/mcube/call", json=conn_payload)

        hangup_payload = {
            "callId": "CALL-001",
            "dialStatus": "ANSWER",
            "endTime": "2026-01-01T12:05:00",
            "answeredTime": "120",
        }
        resp = await runner_client.post("/mcube/call", json=hangup_payload)
        assert resp.status_code == 200

        # Verify status updated in DB
        calls_resp = await runner_client.get("/calls")
        call = calls_resp.json()["calls"][0]
        assert call["status"] == "completed"
        assert call["duration_seconds"] == 120


class TestMcubeWebhookHangupVariants:
    """Test all hangup dial_status values on the /mcube/call endpoint."""

    @pytest.mark.parametrize("dial_status", ["BUSY", "ANSWER", "CANCEL", "NOANSWER"])
    async def test_hangup_statuses_return_ok(self, runner_client, dial_status):
        payload = {"callId": "CALL-HANGUP", "dialStatus": dial_status}
        resp = await runner_client.post("/mcube/call", json=payload)
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"


# ---------------------------------------------------------------------------
# MCube status sync to call_requests
# ---------------------------------------------------------------------------


class TestMcubeStatusSync:
    """Verify that terminal MCube hangup events sync the outcome to call_requests."""

    async def test_busy_hangup_syncs_to_call_requests(
        self, runner_client, phone_number, worker_in_pool, pg_container, test_org_id
    ):
        import psycopg2
        from app.services import call_service
        from app.services import call_request as cr_svc

        # Seed call_request via service layer (satisfies bot_id NOT NULL)
        from app.services import assistant_service

        asst = assistant_service.create(
            name="MCube Sync Bot", system_prompt="t", org_id=test_org_id
        )
        row = cr_svc.create(
            org_id=test_org_id,
            assistant_id=str(asst["id"]),
            phone_number_id=str(phone_number["id"]),
            to_number=TO_NUMBER_FULL,
            priority=5,
        )
        call_request_id = str(row["id"])
        # Advance to initiated
        with psycopg2.connect(pg_container) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE call_requests SET status = 'initiated' WHERE id = %s",
                    (call_request_id,),
                )

        # Create call record with CALL-SYNC-001 as the SID and call_request as parent
        call_service.create(
            call_sid="CALL-SYNC-001",
            direction="outbound",
            from_number=TO_NUMBER_FULL,
            to_number="+914155551234",
            org_id=test_org_id,
            provider="mcube",
            parent_call_sid=call_request_id,
        )
        # Pre-assign worker
        worker_in_pool.current_call_sid = "CALL-SYNC-001"

        # BUSY hangup
        await runner_client.post(
            "/mcube/call",
            json={"callId": "CALL-SYNC-001", "dialStatus": "BUSY"},
        )

        with psycopg2.connect(pg_container) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, call_status FROM call_requests WHERE id = %s",
                    (call_request_id,),
                )
                row = cur.fetchone()
        assert row[0] == "busy"
        assert row[1] == "busy"

    async def test_no_parent_call_sid_does_not_raise(self, runner_client):
        """MCube hangup for an inbound call (no parent_call_sid) should not error."""
        resp = await runner_client.post(
            "/mcube/call",
            json={"callId": "CALL-NO-PARENT", "dialStatus": "BUSY"},
        )
        assert resp.status_code == 200
