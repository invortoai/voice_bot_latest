"""Integration tests for POST /twilio/recording-status webhook.

Covers:
- Happy path: returns 200 {"status": "ok"}
- recording_url stored in calls when RecordingStatus == "completed"
- Non-completed RecordingStatus: URL is NOT stored
- Empty RecordingUrl: silently ignored
- Unknown call_sid: returns 200 without error
- Missing call_sid: handled gracefully
- Sync to call_requests via parent_call_sid when present
"""

import psycopg2
import pytest
from unittest.mock import patch, AsyncMock


@pytest.fixture(autouse=True)
def _bypass_twilio_auth():
    """Bypass Twilio auth for recording integration tests.

    Same pattern as test_twilio_webhook.py. Auth is tested in test_security_*.py.
    """

    with patch("app.routes.twilio._verify_twilio_webhook", new_callable=AsyncMock):
        yield


ASSISTANT_PAYLOAD = {"name": "Twilio Recording Bot", "system_prompt": "test"}
PHONE_PAYLOAD = {
    "phone_number": "+15005550099",
    "provider": "twilio",
    "provider_credentials": {"account_sid": "AC123", "auth_token": "tok"},
    "is_inbound_enabled": True,
}


@pytest.fixture
async def phone_number(runner_client):
    resp = await runner_client.post("/phone-numbers", json=PHONE_PAYLOAD)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _recording_form(
    call_sid, recording_url="https://example.com/rec.mp3", status="completed"
):
    return {
        "CallSid": call_sid,
        "RecordingUrl": recording_url,
        "RecordingStatus": status,
    }


class TestTwilioRecordingStatusWebhook:
    async def test_returns_200_ok(self, runner_client):
        resp = await runner_client.post(
            "/twilio/recording-status",
            data=_recording_form("CA-REC-001"),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_completed_stores_recording_url_in_db(
        self, runner_client, phone_number, worker_in_pool
    ):
        call_sid = "CA-REC-STORE"
        # Create call via incoming webhook
        await runner_client.post(
            "/twilio/incoming",
            data={"CallSid": call_sid, "From": "+14155551234", "To": "+15005550099"},
        )
        # Send recording-status completed
        await runner_client.post(
            "/twilio/recording-status",
            data=_recording_form(
                call_sid, "https://twilio.com/recordings/CA-REC-STORE.mp3"
            ),
        )
        call = (await runner_client.get(f"/calls/{call_sid}")).json()
        assert call["recording_url"] == "https://twilio.com/recordings/CA-REC-STORE.mp3"

    async def test_non_completed_status_does_not_store_url(
        self, runner_client, phone_number, worker_in_pool
    ):
        call_sid = "CA-REC-SKIP"
        await runner_client.post(
            "/twilio/incoming",
            data={"CallSid": call_sid, "From": "+14155551234", "To": "+15005550099"},
        )
        # RecordingStatus = "in-progress" — should NOT store
        await runner_client.post(
            "/twilio/recording-status",
            data=_recording_form(
                call_sid, "https://twilio.com/rec.mp3", status="in-progress"
            ),
        )
        call = (await runner_client.get(f"/calls/{call_sid}")).json()
        assert call.get("recording_url") is None

    async def test_empty_recording_url_is_ignored(
        self, runner_client, phone_number, worker_in_pool
    ):
        call_sid = "CA-REC-EMPTY"
        await runner_client.post(
            "/twilio/incoming",
            data={"CallSid": call_sid, "From": "+14155551234", "To": "+15005550099"},
        )
        await runner_client.post(
            "/twilio/recording-status",
            data={
                "CallSid": call_sid,
                "RecordingStatus": "completed",
                "RecordingUrl": "",
            },
        )
        call = (await runner_client.get(f"/calls/{call_sid}")).json()
        assert call.get("recording_url") is None

    async def test_unknown_call_sid_returns_200(self, runner_client):
        """set_recording_url returns None for unknown sid; endpoint still returns 200."""
        resp = await runner_client.post(
            "/twilio/recording-status",
            data=_recording_form("CA-TOTALLY-UNKNOWN-SID"),
        )
        assert resp.status_code == 200

    async def test_missing_call_sid_does_not_raise(self, runner_client):
        """Omitting CallSid entirely should not cause a 500."""
        resp = await runner_client.post(
            "/twilio/recording-status",
            data={
                "RecordingUrl": "https://example.com/rec.mp3",
                "RecordingStatus": "completed",
            },
        )
        assert resp.status_code == 200

    async def test_syncs_recording_url_to_call_requests(
        self,
        runner_client,
        phone_number,
        worker_in_pool,
        pg_container,
        test_tenant,
        test_org_id,
    ):
        """Recording URL is synced to call_requests via parent_call_sid."""
        from app.services import assistant_service
        from app.services import call_request as call_request_svc
        from app.services import call_service

        # Seed a call_requests row via service layer (satisfies bot_id NOT NULL)
        asst = assistant_service.create(
            name="Rec Sync Bot", system_prompt="test", org_id=test_org_id
        )
        row = call_request_svc.create(
            org_id=test_org_id,
            assistant_id=str(asst["id"]),
            phone_number_id=str(phone_number["id"]),
            to_number="+15005550099",
            priority=5,
        )
        call_request_id = str(row["id"])

        call_sid = "CA-REC-SYNC"
        # Create a call with parent_call_sid = call_request_id
        call_service.create(
            call_sid=call_sid,
            direction="outbound",
            from_number="+15005550099",
            to_number="+14155551234",
            org_id=test_org_id,
            provider="twilio",
            parent_call_sid=call_request_id,
        )

        # Send recording-status
        await runner_client.post(
            "/twilio/recording-status",
            data=_recording_form(call_sid, "https://twilio.com/rec/SYNC.mp3"),
        )

        # Verify call_requests was updated
        with psycopg2.connect(pg_container) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT recording_url FROM call_requests WHERE id = %s",
                    (call_request_id,),
                )
                row = cur.fetchone()
        assert row[0] == "https://twilio.com/rec/SYNC.mp3"
