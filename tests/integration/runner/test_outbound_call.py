"""Integration tests for POST /call/outbound endpoint.

Covers the full lifecycle of initiating an outbound call:
- Validation errors (missing phone, disabled outbound, missing assistant)
- No available workers → 503
- Happy path with mocked provider → returns OutboundCallResponse
- call_id threading: supplied call_id flows into DB and response
- Capacity checks: 429 when phone number concurrent/daily limits exceeded
- Worker is released on provider failure
- Legacy alias /call/outbound/jambonz delegates correctly
- Global API key + X-Org-ID authentication path
"""

import pytest
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

ASSISTANT_PAYLOAD = {
    "name": "Outbound Test Bot",
    "system_prompt": "You are an outbound call assistant.",
}

TWILIO_PHONE_PAYLOAD = {
    "phone_number": "+15005550030",
    "provider": "twilio",
    "provider_credentials": {"account_sid": "AC123", "auth_token": "token123"},
    "is_outbound_enabled": True,
    "is_inbound_enabled": True,
}

MCUBE_PHONE_PAYLOAD = {
    "phone_number": "+15005550031",
    "provider": "mcube",
    "provider_credentials": {"token": "mc-test-token"},
    "is_outbound_enabled": True,
}


@pytest.fixture
def outbound_headers(test_org_id):
    """Headers required by the global-key auth on outbound endpoints."""
    return {"X-Org-ID": test_org_id}


@pytest.fixture
async def assistant(runner_client):
    resp = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
    return resp.json()


@pytest.fixture
async def twilio_phone(runner_client):
    resp = await runner_client.post("/phone-numbers", json=TWILIO_PHONE_PAYLOAD)
    return resp.json()


@pytest.fixture
async def mcube_phone(runner_client):
    resp = await runner_client.post("/phone-numbers", json=MCUBE_PHONE_PAYLOAD)
    return resp.json()


@pytest.fixture
async def outbound_disabled_phone(runner_client):
    payload = {
        **TWILIO_PHONE_PAYLOAD,
        "phone_number": "+15005550032",
        "is_outbound_enabled": False,
    }
    resp = await runner_client.post("/phone-numbers", json=payload)
    return resp.json()


def _mock_twilio_result():
    """Return a mock OutboundCallResult for Twilio."""
    from app.services.outbound.base import OutboundCallResult

    return OutboundCallResult(call_sid="CA-OUTBOUND-001", from_number="+15005550030")


def _mock_mcube_result():
    from app.services.outbound.base import OutboundCallResult

    return OutboundCallResult(call_sid="MCUBE-OUTBOUND-001", from_number="+15005550031")


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------


class TestOutboundCallValidation:
    async def test_unknown_phone_number_id_returns_404(
        self, runner_client, assistant, outbound_headers
    ):
        resp = await runner_client.post(
            "/call/outbound",
            json={
                "phone_number_id": "00000000-0000-0000-0000-000000000000",
                "assistant_id": assistant["id"],
                "to_number": "+14155559999",
            },
            headers=outbound_headers,
        )
        assert resp.status_code == 404

    async def test_outbound_disabled_phone_returns_400(
        self, runner_client, assistant, outbound_disabled_phone, outbound_headers
    ):
        resp = await runner_client.post(
            "/call/outbound",
            json={
                "phone_number_id": outbound_disabled_phone["id"],
                "assistant_id": assistant["id"],
                "to_number": "+14155559999",
            },
            headers=outbound_headers,
        )
        assert resp.status_code == 400
        assert "disabled" in resp.json()["detail"].lower()

    async def test_unknown_assistant_id_returns_404(
        self, runner_client, twilio_phone, outbound_headers
    ):
        resp = await runner_client.post(
            "/call/outbound",
            json={
                "phone_number_id": twilio_phone["id"],
                "assistant_id": "00000000-0000-0000-0000-000000000000",
                "to_number": "+14155559999",
            },
            headers=outbound_headers,
        )
        assert resp.status_code == 404

    async def test_missing_required_fields_returns_422(
        self, runner_client, outbound_headers
    ):
        resp = await runner_client.post(
            "/call/outbound", json={}, headers=outbound_headers
        )
        assert resp.status_code == 422

    async def test_missing_to_number_returns_422(
        self, runner_client, twilio_phone, assistant, outbound_headers
    ):
        resp = await runner_client.post(
            "/call/outbound",
            json={
                "phone_number_id": twilio_phone["id"],
                "assistant_id": assistant["id"],
                # to_number missing
            },
            headers=outbound_headers,
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Worker capacity
# ---------------------------------------------------------------------------


class TestOutboundCallWorkerCapacity:
    async def test_no_available_workers_returns_503(
        self, runner_client, twilio_phone, assistant, outbound_headers
    ):
        """When all workers are busy (or none registered), endpoint returns 503."""
        resp = await runner_client.post(
            "/call/outbound",
            json={
                "phone_number_id": twilio_phone["id"],
                "assistant_id": assistant["id"],
                "to_number": "+14155559999",
            },
            headers=outbound_headers,
        )
        assert resp.status_code == 503
        assert "worker" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Happy path (provider mocked)
# ---------------------------------------------------------------------------


class TestOutboundCallHappyPath:
    async def test_twilio_outbound_returns_call_response(
        self, runner_client, twilio_phone, assistant, worker_in_pool, outbound_headers
    ):
        with patch(
            "app.services.outbound.twilio.TwilioOutboundProvider.initiate",
            new=AsyncMock(return_value=_mock_twilio_result()),
        ):
            resp = await runner_client.post(
                "/call/outbound",
                json={
                    "phone_number_id": twilio_phone["id"],
                    "assistant_id": assistant["id"],
                    "to_number": "+14155559999",
                },
                headers=outbound_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["call_sid"] == "CA-OUTBOUND-001"
        assert data["status"] == "initiated"
        assert data["provider"] == "twilio"

    async def test_outbound_response_contains_call_id(
        self, runner_client, twilio_phone, assistant, worker_in_pool, outbound_headers
    ):
        with patch(
            "app.services.outbound.twilio.TwilioOutboundProvider.initiate",
            new=AsyncMock(return_value=_mock_twilio_result()),
        ):
            resp = await runner_client.post(
                "/call/outbound",
                json={
                    "phone_number_id": twilio_phone["id"],
                    "assistant_id": assistant["id"],
                    "to_number": "+14155559999",
                },
                headers=outbound_headers,
            )
        data = resp.json()
        assert "call_id" in data
        assert data["call_id"] is not None

    async def test_outbound_creates_call_record_in_db(
        self, runner_client, twilio_phone, assistant, worker_in_pool, outbound_headers
    ):
        with patch(
            "app.services.outbound.twilio.TwilioOutboundProvider.initiate",
            new=AsyncMock(return_value=_mock_twilio_result()),
        ):
            await runner_client.post(
                "/call/outbound",
                json={
                    "phone_number_id": twilio_phone["id"],
                    "assistant_id": assistant["id"],
                    "to_number": "+14155559999",
                },
                headers=outbound_headers,
            )
        calls_resp = await runner_client.get("/calls")
        calls = calls_resp.json()["calls"]
        assert len(calls) == 1
        assert calls[0]["direction"] == "outbound"
        assert calls[0]["status"] == "initiated"

    async def test_outbound_with_custom_params(
        self, runner_client, twilio_phone, assistant, worker_in_pool, outbound_headers
    ):
        with patch(
            "app.services.outbound.twilio.TwilioOutboundProvider.initiate",
            new=AsyncMock(return_value=_mock_twilio_result()),
        ):
            resp = await runner_client.post(
                "/call/outbound",
                json={
                    "phone_number_id": twilio_phone["id"],
                    "assistant_id": assistant["id"],
                    "to_number": "+14155559999",
                    "custom_params": {"campaign": "summer-2026", "agent_id": "A001"},
                },
                headers=outbound_headers,
            )
        assert resp.status_code == 200

    async def test_outbound_response_contains_worker_id(
        self, runner_client, twilio_phone, assistant, worker_in_pool, outbound_headers
    ):
        with patch(
            "app.services.outbound.twilio.TwilioOutboundProvider.initiate",
            new=AsyncMock(return_value=_mock_twilio_result()),
        ):
            resp = await runner_client.post(
                "/call/outbound",
                json={
                    "phone_number_id": twilio_phone["id"],
                    "assistant_id": assistant["id"],
                    "to_number": "+14155559999",
                },
                headers=outbound_headers,
            )
        data = resp.json()
        assert data["worker_id"] == worker_in_pool.instance_id


# ---------------------------------------------------------------------------
# Worker cleanup on provider failure
# ---------------------------------------------------------------------------


class TestOutboundCallWorkerCleanup:
    async def test_provider_failure_releases_worker(
        self, runner_client, twilio_phone, assistant, worker_in_pool, outbound_headers
    ):
        """If provider.initiate raises, the reserved worker must be released."""
        from fastapi import HTTPException as FastHTTPException

        with patch(
            "app.services.outbound.twilio.TwilioOutboundProvider.initiate",
            new=AsyncMock(
                side_effect=FastHTTPException(status_code=500, detail="Twilio error")
            ),
        ):
            await runner_client.post(
                "/call/outbound",
                json={
                    "phone_number_id": twilio_phone["id"],
                    "assistant_id": assistant["id"],
                    "to_number": "+14155559999",
                },
                headers=outbound_headers,
            )

        # Worker should be free again after failure
        assert worker_in_pool.is_accepting_calls
        assert worker_in_pool.current_call_sid is None


# ---------------------------------------------------------------------------
# call_id threading — supplied UUID flows end-to-end
# ---------------------------------------------------------------------------


class TestOutboundCallIdThreading:
    async def test_supplied_call_id_returned_in_response(
        self, runner_client, twilio_phone, assistant, worker_in_pool, outbound_headers
    ):
        """When call_id is provided, the same UUID must appear in the response."""
        import uuid

        pre_assigned = str(uuid.uuid4())
        with patch(
            "app.services.outbound.twilio.TwilioOutboundProvider.initiate",
            new=AsyncMock(return_value=_mock_twilio_result()),
        ):
            resp = await runner_client.post(
                "/call/outbound",
                json={
                    "phone_number_id": twilio_phone["id"],
                    "assistant_id": assistant["id"],
                    "to_number": "+14155559999",
                    "call_id": pre_assigned,
                },
                headers=outbound_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["call_id"] == pre_assigned

    async def test_supplied_call_id_stored_as_calls_id_in_db(
        self, runner_client, twilio_phone, assistant, worker_in_pool, outbound_headers
    ):
        """The supplied call_id must be stored as calls.id (not a new UUID)."""
        import uuid

        pre_assigned = str(uuid.uuid4())
        with patch(
            "app.services.outbound.twilio.TwilioOutboundProvider.initiate",
            new=AsyncMock(return_value=_mock_twilio_result()),
        ):
            await runner_client.post(
                "/call/outbound",
                json={
                    "phone_number_id": twilio_phone["id"],
                    "assistant_id": assistant["id"],
                    "to_number": "+14155559999",
                    "call_id": pre_assigned,
                },
                headers=outbound_headers,
            )
        call_resp = await runner_client.get(f"/calls/{pre_assigned}")
        assert call_resp.status_code == 200
        assert call_resp.json()["id"] == pre_assigned

    async def test_omitting_call_id_generates_uuid(
        self, runner_client, twilio_phone, assistant, worker_in_pool, outbound_headers
    ):
        """When call_id is omitted, a valid UUID is auto-generated."""
        import uuid

        with patch(
            "app.services.outbound.twilio.TwilioOutboundProvider.initiate",
            new=AsyncMock(return_value=_mock_twilio_result()),
        ):
            resp = await runner_client.post(
                "/call/outbound",
                json={
                    "phone_number_id": twilio_phone["id"],
                    "assistant_id": assistant["id"],
                    "to_number": "+14155559999",
                },
                headers=outbound_headers,
            )
        data = resp.json()
        assert data["call_id"] is not None
        uuid.UUID(data["call_id"])  # raises ValueError if not a valid UUID

    async def test_call_id_with_lead_details_in_custom_params(
        self, runner_client, twilio_phone, assistant, worker_in_pool, outbound_headers
    ):
        """Lead details passed in custom_params are stored in the calls record."""
        import uuid

        pre_assigned = str(uuid.uuid4())
        custom_params = {
            "first_name": "Rahul",
            "last_name": "Sharma",
            "lead_id": "LEAD001",
            "city": "Delhi",
        }
        with patch(
            "app.services.outbound.twilio.TwilioOutboundProvider.initiate",
            new=AsyncMock(return_value=_mock_twilio_result()),
        ):
            resp = await runner_client.post(
                "/call/outbound",
                json={
                    "phone_number_id": twilio_phone["id"],
                    "assistant_id": assistant["id"],
                    "to_number": "+914155559999",
                    "call_id": pre_assigned,
                    "custom_params": custom_params,
                },
                headers=outbound_headers,
            )
        assert resp.status_code == 200
        call_resp = await runner_client.get(f"/calls/{pre_assigned}")
        stored = call_resp.json()
        assert stored["custom_params"]["first_name"] == "Rahul"
        assert stored["custom_params"]["lead_id"] == "LEAD001"


# ---------------------------------------------------------------------------
# Phone number capacity checks → 429
# ---------------------------------------------------------------------------


class TestOutboundCallCapacityLimits:
    async def test_concurrent_limit_returns_429(
        self, runner_client, twilio_phone, assistant, worker_in_pool, outbound_headers
    ):
        """When active calls >= max_concurrent_calls, endpoint returns 429."""
        with (
            patch("app.services.call_service.count_active_calls", return_value=5),
            patch("app.routes.calls.call_service.count_active_calls", return_value=5),
        ):
            # Set a phone with max_concurrent_calls=5 via DB
            from app.core.database import get_cursor

            with get_cursor() as cur:
                cur.execute(
                    "UPDATE phone_numbers SET max_concurrent_calls = 5 WHERE id = %s",
                    (twilio_phone["id"],),
                )

            resp = await runner_client.post(
                "/call/outbound",
                json={
                    "phone_number_id": twilio_phone["id"],
                    "assistant_id": assistant["id"],
                    "to_number": "+14155559999",
                },
                headers=outbound_headers,
            )
        assert resp.status_code == 429
        assert "concurrent" in resp.json()["detail"].lower()

    async def test_daily_limit_returns_429(
        self, runner_client, twilio_phone, assistant, worker_in_pool, outbound_headers
    ):
        """When today's call count >= max_calls_per_day, endpoint returns 429."""
        from app.core.database import get_cursor
        from app.services import call_service as cs

        # Set a low daily limit
        with get_cursor() as cur:
            cur.execute(
                "UPDATE phone_numbers SET max_calls_per_day = 2 WHERE id = %s",
                (twilio_phone["id"],),
            )

        with patch.object(cs, "count_calls_today", return_value=2):
            resp = await runner_client.post(
                "/call/outbound",
                json={
                    "phone_number_id": twilio_phone["id"],
                    "assistant_id": assistant["id"],
                    "to_number": "+14155559999",
                },
                headers=outbound_headers,
            )
        assert resp.status_code == 429
        assert "daily" in resp.json()["detail"].lower()

    async def test_inactive_phone_returns_400(
        self, runner_client, assistant, outbound_headers
    ):
        """A phone number with is_active=False cannot be used."""
        resp = await runner_client.post(
            "/phone-numbers",
            json={
                "phone_number": "+15005550099",
                "provider": "twilio",
                "provider_credentials": {"account_sid": "AC123", "auth_token": "t"},
                "is_outbound_enabled": True,
            },
        )
        inactive_phone = resp.json()

        from app.core.database import get_cursor

        with get_cursor() as cur:
            cur.execute(
                "UPDATE phone_numbers SET is_active = FALSE WHERE id = %s",
                (inactive_phone["id"],),
            )

        resp2 = await runner_client.post(
            "/call/outbound",
            json={
                "phone_number_id": inactive_phone["id"],
                "assistant_id": assistant["id"],
                "to_number": "+14155559999",
            },
            headers=outbound_headers,
        )
        assert resp2.status_code == 400
        assert "inactive" in resp2.json()["detail"].lower()


class TestOutboundCallLegacyAlias:
    async def test_jambonz_alias_endpoint_exists(
        self, runner_client, twilio_phone, assistant, worker_in_pool, outbound_headers
    ):
        """POST /call/outbound/jambonz is a deprecated alias that still works."""
        with patch(
            "app.services.outbound.twilio.TwilioOutboundProvider.initiate",
            new=AsyncMock(return_value=_mock_twilio_result()),
        ):
            resp = await runner_client.post(
                "/call/outbound/jambonz",
                json={
                    "phone_number_id": twilio_phone["id"],
                    "assistant_id": assistant["id"],
                    "to_number": "+14155559999",
                },
                headers=outbound_headers,
            )
        assert resp.status_code == 200
