"""Integration tests for call rate-limiting functions in app.services.call_service.

Tests:
  - count_active_calls: returns live count of initiated/in-progress calls
  - count_calls_today: returns count of calls created today (UTC)

These tests run against the real Postgres container seeded by conftest.py.
"""

import pytest
from app.services import call_service
from app.core.database import get_cursor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_call(
    call_sid: str, phone_number_id: str, org_id: str, status: str = "initiated"
):
    """Insert a minimal call record directly via call_service.create."""
    call_service.create(
        call_sid=call_sid,
        direction="outbound",
        from_number="+15005550030",
        to_number="+919999999999",
        org_id=org_id,
        phone_number_id=phone_number_id,
        status=status,
    )


@pytest.fixture
async def phone(runner_client):
    resp = await runner_client.post(
        "/phone-numbers",
        json={
            "phone_number": "+15005550040",
            "provider": "twilio",
            "provider_credentials": {"account_sid": "AC999", "auth_token": "tok"},
            "is_outbound_enabled": True,
        },
    )
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# count_active_calls
# ---------------------------------------------------------------------------


class TestCountActiveCalls:
    def test_returns_zero_when_no_calls(self, phone, test_org_id):
        result = call_service.count_active_calls(
            phone_number_id=phone["id"], org_id=test_org_id
        )
        assert result == 0

    def test_counts_initiated_calls(self, phone, test_org_id):
        _make_call("CA-ACTIVE-001", phone["id"], test_org_id, status="initiated")
        result = call_service.count_active_calls(
            phone_number_id=phone["id"], org_id=test_org_id
        )
        assert result == 1

    def test_counts_in_progress_calls(self, phone, test_org_id):
        _make_call("CA-ACTIVE-002", phone["id"], test_org_id, status="initiated")
        # Manually update to in-progress
        with get_cursor() as cur:
            cur.execute(
                "UPDATE calls SET status = 'in-progress' WHERE call_sid = %s",
                ("CA-ACTIVE-002",),
            )
        result = call_service.count_active_calls(
            phone_number_id=phone["id"], org_id=test_org_id
        )
        assert result == 1

    def test_does_not_count_completed_calls(self, phone, test_org_id):
        _make_call("CA-DONE-001", phone["id"], test_org_id, status="initiated")
        call_service.update_status("CA-DONE-001", "completed", duration_seconds=60)
        result = call_service.count_active_calls(
            phone_number_id=phone["id"], org_id=test_org_id
        )
        assert result == 0

    def test_does_not_count_failed_calls(self, phone, test_org_id):
        _make_call("CA-FAIL-001", phone["id"], test_org_id, status="initiated")
        call_service.update_status("CA-FAIL-001", "failed")
        result = call_service.count_active_calls(
            phone_number_id=phone["id"], org_id=test_org_id
        )
        assert result == 0

    def test_counts_multiple_active_calls(self, phone, test_org_id):
        _make_call("CA-MULTI-001", phone["id"], test_org_id, status="initiated")
        _make_call("CA-MULTI-002", phone["id"], test_org_id, status="initiated")
        _make_call("CA-MULTI-003", phone["id"], test_org_id, status="initiated")
        result = call_service.count_active_calls(
            phone_number_id=phone["id"], org_id=test_org_id
        )
        assert result == 3

    def test_scoped_to_org_id(self, phone, test_org_id):
        """Active calls from a different org must not be counted."""
        _make_call("CA-ORG-001", phone["id"], test_org_id, status="initiated")
        result = call_service.count_active_calls(
            phone_number_id=phone["id"],
            org_id="00000000-0000-0000-0000-000000000000",  # different org
        )
        assert result == 0

    def test_scoped_to_phone_number_id(self, runner_client, test_org_id, phone):
        """Active calls on a different phone number must not be counted."""
        _make_call("CA-PHONE-001", phone["id"], test_org_id, status="initiated")
        result = call_service.count_active_calls(
            phone_number_id="00000000-0000-0000-0000-000000000000",  # different number
            org_id=test_org_id,
        )
        assert result == 0


# ---------------------------------------------------------------------------
# count_calls_today
# ---------------------------------------------------------------------------


class TestCountCallsToday:
    def test_returns_zero_when_no_calls(self, phone, test_org_id):
        result = call_service.count_calls_today(
            phone_number_id=phone["id"], org_id=test_org_id
        )
        assert result == 0

    def test_counts_call_created_today(self, phone, test_org_id):
        _make_call("CA-TODAY-001", phone["id"], test_org_id)
        result = call_service.count_calls_today(
            phone_number_id=phone["id"], org_id=test_org_id
        )
        assert result == 1

    def test_counts_multiple_calls_today(self, phone, test_org_id):
        _make_call("CA-TODAY-002", phone["id"], test_org_id)
        _make_call("CA-TODAY-003", phone["id"], test_org_id)
        _make_call("CA-TODAY-004", phone["id"], test_org_id)
        result = call_service.count_calls_today(
            phone_number_id=phone["id"], org_id=test_org_id
        )
        assert result == 3

    def test_excludes_calls_from_yesterday(self, phone, test_org_id):
        """Calls with created_at set to yesterday must not be counted."""
        _make_call("CA-YEST-001", phone["id"], test_org_id)
        with get_cursor() as cur:
            cur.execute(
                "UPDATE calls SET created_at = NOW() - INTERVAL '1 day' WHERE call_sid = %s",
                ("CA-YEST-001",),
            )
        result = call_service.count_calls_today(
            phone_number_id=phone["id"], org_id=test_org_id
        )
        assert result == 0

    def test_scoped_to_org_id(self, phone, test_org_id):
        _make_call("CA-ORGD-001", phone["id"], test_org_id)
        result = call_service.count_calls_today(
            phone_number_id=phone["id"],
            org_id="00000000-0000-0000-0000-000000000000",
        )
        assert result == 0

    def test_counts_all_statuses_today(self, phone, test_org_id):
        """Daily limit counts all calls regardless of final status."""
        _make_call("CA-STAT-001", phone["id"], test_org_id, status="initiated")
        _make_call("CA-STAT-002", phone["id"], test_org_id, status="initiated")
        call_service.update_status("CA-STAT-002", "completed", duration_seconds=45)
        result = call_service.count_calls_today(
            phone_number_id=phone["id"], org_id=test_org_id
        )
        assert result == 2
