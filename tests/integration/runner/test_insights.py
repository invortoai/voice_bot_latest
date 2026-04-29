"""Integration tests for GET /call-stats and related endpoints.

Tests cover:
- GET /call-stats  — list terminal call stats with filtering/pagination
- GET /call-stats/{request_id}  — single record lookup + 404 + org isolation
- GET /call-stats/{request_id}/webhook-deliveries  — delivery log

All endpoints use verify_customer_api_key (per-org X-API-Key header).
"""

import uuid

import psycopg2
import pytest
from psycopg2.extras import Json

# Counter for unique phone-number suffixes per test run
_SUFFIX_COUNTER = 0


def _next_suffix():
    global _SUFFIX_COUNTER
    _SUFFIX_COUNTER += 1
    return f"{_SUFFIX_COUNTER:04d}"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _ensure_org(pg_container, org_id):
    """Create a minimal organization row if it does not already exist."""
    with psycopg2.connect(pg_container) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM organizations WHERE id = %s", (org_id,))
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO organizations (id, name, org_type, is_active)
                    VALUES (%s, 'Other Org', 'demo', TRUE)
                    """,
                    (org_id,),
                )


def _seed_call_request(
    pg_container,
    org_id,
    lifecycle_status="completed",
    call_status="completed",
    duration=120,
):
    """Create a call_requests row via the service layer then update to terminal status.

    Uses the service to satisfy the NOT NULL bot_id FK constraint, then
    updates status/call_status/duration via SQL.
    """
    from app.services import assistant_service, phone_number_service
    from app.services import call_request as call_request_service

    _ensure_org(pg_container, org_id)

    suffix = _next_suffix()
    to_number = f"+9170229{suffix}"

    asst = assistant_service.create(
        name=f"Stats Bot {suffix}",
        system_prompt="test",
        org_id=org_id,
    )
    phone = phone_number_service.create(
        phone_number=to_number,
        org_id=org_id,
        provider="twilio",
        provider_credentials={"account_sid": "AC123", "auth_token": "tok"},
    )
    row = call_request_service.create(
        org_id=org_id,
        assistant_id=str(asst["id"]),
        phone_number_id=str(phone["id"]),
        to_number=to_number,
        priority=5,
    )
    request_id = str(row["id"])

    with psycopg2.connect(pg_container) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE call_requests
                SET status = %s, call_status = %s, call_duration_seconds = %s
                WHERE id = %s
                """,
                (lifecycle_status, call_status, duration, request_id),
            )
    return request_id


def _seed_webhook_delivery(pg_container, call_request_id):
    """Insert a webhook_deliveries row and return its UUID."""
    with psycopg2.connect(pg_container) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Look up the org_id from the call_request (required NOT NULL)
            cur.execute(
                "SELECT org_id FROM call_requests WHERE id = %s", (call_request_id,)
            )
            org_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO webhook_deliveries (
                    org_id, call_request_id, event_type, webhook_url,
                    payload, status, attempt_number, max_attempts
                )
                VALUES (%s, %s, 'call_completed', 'https://example.com/wh',
                        %s, 'pending', 1, 3)
                RETURNING id
                """,
                (org_id, call_request_id, Json({"event": "call_completed"})),
            )
            return str(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# GET /call-stats
# ---------------------------------------------------------------------------


class TestListCallStats:
    async def test_returns_empty_list_when_no_terminal_calls(self, runner_client):
        resp = await runner_client.get("/call-stats")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_terminal_calls(
        self, runner_client, pg_container, test_org_id
    ):
        _seed_call_request(pg_container, test_org_id)
        resp = await runner_client.get("/call-stats")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_queued_calls_not_included(
        self, runner_client, pg_container, test_org_id
    ):
        """status IN ('queued','processing','initiated') are excluded from stats."""
        completed_id = _seed_call_request(
            pg_container, test_org_id, lifecycle_status="completed"
        )
        # Also seed a queued one via service (status=queued is the default from create())
        from app.services import assistant_service, phone_number_service
        from app.services import call_request as call_request_service

        suffix = _next_suffix()
        to_num = f"+9170229{suffix}"
        asst = assistant_service.create(
            name=f"Queued Bot {suffix}", system_prompt="t", org_id=test_org_id
        )
        ph = phone_number_service.create(
            phone_number=to_num,
            org_id=test_org_id,
            provider="twilio",
            provider_credentials={"account_sid": "A", "auth_token": "b"},
        )
        call_request_service.create(
            org_id=test_org_id,
            assistant_id=str(asst["id"]),
            phone_number_id=str(ph["id"]),
            to_number=to_num,
            priority=5,
        )

        resp = await runner_client.get("/call-stats")
        assert len(resp.json()) == 1  # only the terminal one

    async def test_filter_by_call_status_answered(
        self, runner_client, pg_container, test_org_id
    ):
        """?call_status=answered maps to DB call_status='completed'."""
        _seed_call_request(pg_container, test_org_id, call_status="completed")
        _seed_call_request(
            pg_container,
            test_org_id,
            lifecycle_status="no-answer",
            call_status="no-answer",
        )
        resp = await runner_client.get("/call-stats?call_status=answered")
        results = resp.json()
        assert len(results) == 1
        assert results[0]["call_status"] == "answered"

    async def test_filter_by_call_status_missed(
        self, runner_client, pg_container, test_org_id
    ):
        """?call_status=missed maps to DB call_status='no-answer'."""
        _seed_call_request(pg_container, test_org_id, call_status="completed")
        _seed_call_request(
            pg_container,
            test_org_id,
            lifecycle_status="no-answer",
            call_status="no-answer",
        )
        resp = await runner_client.get("/call-stats?call_status=missed")
        results = resp.json()
        assert len(results) == 1
        assert results[0]["call_status"] == "missed"

    async def test_filter_by_call_status_busy(
        self, runner_client, pg_container, test_org_id
    ):
        _seed_call_request(
            pg_container, test_org_id, lifecycle_status="busy", call_status="busy"
        )
        _seed_call_request(pg_container, test_org_id, call_status="completed")
        resp = await runner_client.get("/call-stats?call_status=busy")
        results = resp.json()
        assert len(results) == 1
        assert results[0]["call_status"] == "busy"

    async def test_filter_by_call_status_failed(
        self, runner_client, pg_container, test_org_id
    ):
        _seed_call_request(
            pg_container, test_org_id, lifecycle_status="failed", call_status="failed"
        )
        _seed_call_request(pg_container, test_org_id, call_status="completed")
        resp = await runner_client.get("/call-stats?call_status=failed")
        results = resp.json()
        assert len(results) == 1
        assert results[0]["call_status"] == "failed"

    async def test_filter_by_call_status_rejected(
        self, runner_client, pg_container, test_org_id
    ):
        """?call_status=rejected maps to DB call_status='cancelled'."""
        _seed_call_request(
            pg_container,
            test_org_id,
            lifecycle_status="cancelled",
            call_status="cancelled",
        )
        _seed_call_request(pg_container, test_org_id, call_status="completed")
        resp = await runner_client.get("/call-stats?call_status=rejected")
        results = resp.json()
        assert len(results) == 1
        assert results[0]["call_status"] == "rejected"

    async def test_response_has_required_fields(
        self, runner_client, pg_container, test_org_id
    ):
        _seed_call_request(pg_container, test_org_id)
        record = (await runner_client.get("/call-stats")).json()[0]
        for field in [
            "request_id",
            "call_status",
            "total_duration_seconds",
            "initiation_payload",
        ]:
            assert field in record, f"Missing field: {field}"

    async def test_duration_seconds_returned(
        self, runner_client, pg_container, test_org_id
    ):
        _seed_call_request(pg_container, test_org_id, duration=180)
        resp = await runner_client.get("/call-stats")
        assert resp.json()[0]["total_duration_seconds"] == 180

    async def test_initiation_payload_is_dict(
        self, runner_client, pg_container, test_org_id
    ):
        _seed_call_request(pg_container, test_org_id)
        record = (await runner_client.get("/call-stats")).json()[0]
        assert isinstance(record["initiation_payload"], dict)

    async def test_org_isolation(self, runner_client, pg_container, test_org_id):
        """Records belonging to a different org are not returned."""
        other_org_id = str(uuid.uuid4())
        _seed_call_request(pg_container, other_org_id)
        resp = await runner_client.get("/call-stats")
        assert resp.json() == []

    async def test_pagination_limit(self, runner_client, pg_container, test_org_id):
        for _ in range(5):
            _seed_call_request(pg_container, test_org_id)
        resp = await runner_client.get("/call-stats?limit=2")
        assert len(resp.json()) == 2

    async def test_pagination_offset(self, runner_client, pg_container, test_org_id):
        for _ in range(5):
            _seed_call_request(pg_container, test_org_id)
        all_results = (await runner_client.get("/call-stats")).json()
        offset_results = (await runner_client.get("/call-stats?offset=2")).json()
        assert len(offset_results) == len(all_results) - 2


# ---------------------------------------------------------------------------
# GET /call-stats/{request_id}
# ---------------------------------------------------------------------------


class TestGetSingleCallStat:
    async def test_returns_200_with_record(
        self, runner_client, pg_container, test_org_id
    ):
        request_id = _seed_call_request(pg_container, test_org_id)
        resp = await runner_client.get(f"/call-stats/{request_id}")
        assert resp.status_code == 200
        assert resp.json()["request_id"] == request_id

    async def test_returns_expected_call_status(
        self, runner_client, pg_container, test_org_id
    ):
        """completed in DB → answered in API response."""
        request_id = _seed_call_request(
            pg_container,
            test_org_id,
            lifecycle_status="completed",
            call_status="completed",
        )
        resp = await runner_client.get(f"/call-stats/{request_id}")
        assert resp.json()["call_status"] == "answered"

    async def test_not_found_returns_404(self, runner_client):
        resp = await runner_client.get(f"/call-stats/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_org_isolation_returns_404(self, runner_client, pg_container):
        """Record belonging to a different org is not returned."""
        other_org_id = str(uuid.uuid4())
        request_id = _seed_call_request(pg_container, other_org_id)
        resp = await runner_client.get(f"/call-stats/{request_id}")
        assert resp.status_code == 404

    async def test_invalid_uuid_returns_422(self, runner_client):
        resp = await runner_client.get("/call-stats/not-a-uuid")
        assert resp.status_code == 422

    async def test_response_contains_initiation_payload(
        self, runner_client, pg_container, test_org_id
    ):
        request_id = _seed_call_request(pg_container, test_org_id)
        resp = await runner_client.get(f"/call-stats/{request_id}")
        assert "initiation_payload" in resp.json()

    async def test_duration_returned(self, runner_client, pg_container, test_org_id):
        request_id = _seed_call_request(pg_container, test_org_id, duration=300)
        resp = await runner_client.get(f"/call-stats/{request_id}")
        assert resp.json()["total_duration_seconds"] == 300


# ---------------------------------------------------------------------------
# GET /call-stats/{request_id}/webhook-deliveries
# ---------------------------------------------------------------------------


class TestGetCallWebhookDeliveries:
    async def test_empty_list_when_no_deliveries(
        self, runner_client, pg_container, test_org_id
    ):
        request_id = _seed_call_request(pg_container, test_org_id)
        resp = await runner_client.get(f"/call-stats/{request_id}/webhook-deliveries")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_seeded_delivery(
        self, runner_client, pg_container, test_org_id
    ):
        request_id = _seed_call_request(pg_container, test_org_id)
        _seed_webhook_delivery(pg_container, request_id)
        resp = await runner_client.get(f"/call-stats/{request_id}/webhook-deliveries")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_delivery_has_expected_fields(
        self, runner_client, pg_container, test_org_id
    ):
        request_id = _seed_call_request(pg_container, test_org_id)
        _seed_webhook_delivery(pg_container, request_id)
        delivery = (
            await runner_client.get(f"/call-stats/{request_id}/webhook-deliveries")
        ).json()[0]
        for field in ["id", "call_request_id", "event_type", "webhook_url", "status"]:
            assert field in delivery, f"Missing field: {field}"

    async def test_delivery_fields_match_seeded_data(
        self, runner_client, pg_container, test_org_id
    ):
        request_id = _seed_call_request(pg_container, test_org_id)
        _seed_webhook_delivery(pg_container, request_id)
        delivery = (
            await runner_client.get(f"/call-stats/{request_id}/webhook-deliveries")
        ).json()[0]
        assert delivery["event_type"] == "call_completed"
        assert delivery["webhook_url"] == "https://example.com/wh"
        assert delivery["attempt_number"] == 1
        assert delivery["max_attempts"] == 3

    async def test_multiple_deliveries_returned(
        self, runner_client, pg_container, test_org_id
    ):
        request_id = _seed_call_request(pg_container, test_org_id)
        _seed_webhook_delivery(pg_container, request_id)
        _seed_webhook_delivery(pg_container, request_id)
        deliveries = (
            await runner_client.get(f"/call-stats/{request_id}/webhook-deliveries")
        ).json()
        assert len(deliveries) == 2

    async def test_org_isolation(self, runner_client, pg_container):
        """Deliveries for a different org's call_request return empty list."""
        other_org_id = str(uuid.uuid4())
        request_id = _seed_call_request(pg_container, other_org_id)
        _seed_webhook_delivery(pg_container, request_id)
        resp = await runner_client.get(f"/call-stats/{request_id}/webhook-deliveries")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_invalid_uuid_returns_422(self, runner_client):
        resp = await runner_client.get("/call-stats/not-a-uuid/webhook-deliveries")
        assert resp.status_code == 422
