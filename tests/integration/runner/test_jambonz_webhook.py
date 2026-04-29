"""Integration tests for Jambonz webhook endpoints.

Covers:
- POST /jambonz/call  — inbound and outbound call routing; returns verb list
- POST /jambonz/status — status mapping; worker release on terminal states
- Webhook authentication (DAAI-138) — Bearer token enforcement in production mode
- Latency metadata (DAAI-217 merge) — runner_webhook_ms / webhook_completed_at present

Jambonz sends JSON payloads using camelCase field names.
"""

import pytest
from contextlib import contextmanager
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Auth bypass for non-auth tests — Jambonz auth is enforced everywhere now.
# Tests in TestJambonzWebhookAuthentication test the auth itself; all other
# tests patch it away so they can focus on call routing logic.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_jambonz_auth():
    """Bypass Jambonz webhook auth for all tests except the auth test class."""
    with patch("app.routes.jambonz._verify_jambonz_webhook", return_value=None):
        yield


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

ASSISTANT_PAYLOAD = {
    "name": "Jambonz Test Bot",
    "system_prompt": "You are a helpful assistant.",
}

PHONE_PAYLOAD = {
    "phone_number": "+15005550020",
    "provider": "jambonz",
    "provider_credentials": {"trunk_name": "test-trunk"},
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


def _inbound_payload(
    call_sid="JSID-001",
    from_="+14155551234",
    to="+15005550020",
    direction="inbound",
    extra=None,
):
    payload = {
        "callSid": call_sid,
        "from": from_,
        "to": to,
        "direction": direction,
    }
    if extra:
        payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# /jambonz/call — inbound path
# ---------------------------------------------------------------------------


class TestJambonzCallWebhookInbound:
    async def test_inbound_with_worker_returns_200(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/jambonz/call",
            json=_inbound_payload(to="+15005550020"),
        )
        assert resp.status_code == 200

    async def test_inbound_response_is_list_of_verbs(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/jambonz/call",
            json=_inbound_payload(to="+15005550020"),
        )
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 2

    async def test_inbound_response_starts_with_answer_verb(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/jambonz/call",
            json=_inbound_payload(to="+15005550020"),
        )
        verbs = resp.json()
        assert verbs[0]["verb"] == "answer"

    async def test_inbound_response_contains_listen_verb(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/jambonz/call",
            json=_inbound_payload(to="+15005550020"),
        )
        verbs = resp.json()
        listen_verbs = [v for v in verbs if v.get("verb") == "listen"]
        assert len(listen_verbs) == 1

    async def test_inbound_listen_verb_has_ws_url(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/jambonz/call",
            json=_inbound_payload(to="+15005550020"),
        )
        listen_verb = next(v for v in resp.json() if v.get("verb") == "listen")
        assert "url" in listen_verb
        assert "ws" in listen_verb["url"].lower()

    async def test_inbound_listen_verb_has_bidirectional_audio(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/jambonz/call",
            json=_inbound_payload(to="+15005550020"),
        )
        listen_verb = next(v for v in resp.json() if v.get("verb") == "listen")
        assert "bidirectionalAudio" in listen_verb
        assert listen_verb["bidirectionalAudio"]["enabled"] is True

    async def test_inbound_listen_verb_has_metadata(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/jambonz/call",
            json=_inbound_payload(call_sid="JSID-META", to="+15005550020"),
        )
        listen_verb = next(v for v in resp.json() if v.get("verb") == "listen")
        metadata = listen_verb["metadata"]
        assert metadata["call_sid"] == "JSID-META"
        assert metadata["call_type"] == "inbound"

    async def test_inbound_listen_verb_jambonz_path(
        self, runner_client, phone_number, worker_in_pool
    ):
        """Jambonz calls must use /ws/jambonz, not /ws."""
        resp = await runner_client.post(
            "/jambonz/call",
            json=_inbound_payload(to="+15005550020"),
        )
        listen_verb = next(v for v in resp.json() if v.get("verb") == "listen")
        assert "/ws/jambonz" in listen_verb["url"]

    async def test_inbound_worker_assigned(
        self, runner_client, phone_number, worker_in_pool
    ):
        await runner_client.post(
            "/jambonz/call",
            json=_inbound_payload(call_sid="JSID-ASSIGN", to="+15005550020"),
        )
        assert not worker_in_pool.is_accepting_calls

    async def test_inbound_creates_call_record(
        self, runner_client, phone_number, worker_in_pool
    ):
        await runner_client.post(
            "/jambonz/call",
            json=_inbound_payload(call_sid="JSID-DB-CREATE", to="+15005550020"),
        )
        calls_resp = await runner_client.get("/calls")
        calls = calls_resp.json()["calls"]
        assert len(calls) == 1
        assert calls[0]["call_sid"] == "JSID-DB-CREATE"
        assert calls[0]["direction"] == "inbound"
        assert calls[0]["provider"] == "jambonz"

    async def test_inbound_no_worker_returns_busy_response(
        self, runner_client, phone_number
    ):
        """When no worker is available, returns [answer, say, hangup] verbs."""
        resp = await runner_client.post(
            "/jambonz/call",
            json=_inbound_payload(to="+15005550020"),
        )
        assert resp.status_code == 200
        verbs = resp.json()
        verb_names = [v.get("verb") for v in verbs]
        assert "say" in verb_names
        assert "hangup" in verb_names

    async def test_inbound_sample_rate_defaults_to_8000(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/jambonz/call",
            json=_inbound_payload(to="+15005550020"),
        )
        listen_verb = next(v for v in resp.json() if v.get("verb") == "listen")
        assert listen_verb["sampleRate"] == 8000

    async def test_inbound_custom_sample_rate_respected(
        self, runner_client, phone_number, worker_in_pool
    ):
        payload = {**_inbound_payload(to="+15005550020"), "sampleRate": 16000}
        resp = await runner_client.post("/jambonz/call", json=payload)
        listen_verb = next(v for v in resp.json() if v.get("verb") == "listen")
        assert listen_verb["sampleRate"] == 16000


# ---------------------------------------------------------------------------
# /jambonz/call — outbound path
# ---------------------------------------------------------------------------


class TestJambonzCallWebhookOutbound:
    async def test_outbound_answered_uses_pre_assigned_worker(
        self, runner_client, worker_in_pool
    ):
        """For outbound, the worker is already assigned; webhook just returns verbs."""
        import json

        # Simulate pre-assignment via pool
        worker_in_pool.current_call_sid = "JSID-OUT-001"

        payload = _inbound_payload(
            call_sid="JSID-OUT-001",
            from_="+15005550020",
            to="+14155551234",
            direction="outbound",
            extra={"customerData": json.dumps({"assistant_id": "asst-001"})},
        )
        resp = await runner_client.post("/jambonz/call", json=payload)
        assert resp.status_code == 200
        verbs = resp.json()
        assert any(v.get("verb") == "answer" for v in verbs)

    async def test_outbound_metadata_call_type_is_outbound(
        self, runner_client, worker_in_pool
    ):
        worker_in_pool.current_call_sid = "JSID-OUT-META"

        payload = _inbound_payload(
            call_sid="JSID-OUT-META",
            direction="outbound",
        )
        resp = await runner_client.post("/jambonz/call", json=payload)
        listen_verb = next(v for v in resp.json() if v.get("verb") == "listen")
        assert listen_verb["metadata"]["call_type"] == "outbound"


# ---------------------------------------------------------------------------
# /jambonz/status tests
# ---------------------------------------------------------------------------


class TestJambonzStatusWebhook:
    async def test_status_returns_ok(self, runner_client):
        resp = await runner_client.post(
            "/jambonz/status",
            json={"callSid": "JSID-STAT-001", "callStatus": "completed"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @pytest.mark.parametrize(
        "jambonz_status,expected_db_status",
        [
            ("completed", "completed"),
            ("ended", "completed"),
            ("hangup", "completed"),
            ("hangup_complete", "completed"),
            ("failed", "failed"),
            ("error", "failed"),
            ("canceled", "cancelled"),
            ("cancelled", "cancelled"),
            ("busy", "busy"),
            ("no-answer", "no-answer"),
            ("in-progress", "in-progress"),
            ("answered", "in-progress"),
        ],
    )
    async def test_jambonz_status_maps_to_correct_db_status(
        self,
        runner_client,
        jambonz_status,
        expected_db_status,
    ):
        """Jambonz status values are normalized before writing to the DB."""
        from app.services import call_service

        call_service.create(
            call_sid=f"JSID-MAP-{jambonz_status}",
            direction="inbound",
            from_number="+1111",
            to_number="+2222",
            provider="jambonz",
        )
        await runner_client.post(
            "/jambonz/status",
            json={
                "callSid": f"JSID-MAP-{jambonz_status}",
                "callStatus": jambonz_status,
            },
        )
        call = call_service.get_by_sid(f"JSID-MAP-{jambonz_status}")
        assert call["status"] == expected_db_status

    @pytest.mark.parametrize(
        "terminal_status",
        [
            "completed",
            "ended",
            "hangup",
            "hangup_complete",
            "failed",
            "error",
            "busy",
            "no-answer",
            "canceled",
            "cancelled",
        ],
    )
    async def test_terminal_statuses_release_worker(
        self, runner_client, terminal_status, worker_in_pool
    ):
        worker_in_pool.current_call_sid = f"JSID-TERM-{terminal_status}"

        await runner_client.post(
            "/jambonz/status",
            json={
                "callSid": f"JSID-TERM-{terminal_status}",
                "callStatus": terminal_status,
            },
        )
        assert worker_in_pool.is_accepting_calls

    async def test_non_terminal_status_does_not_release_worker(
        self, runner_client, worker_in_pool
    ):
        worker_in_pool.current_call_sid = "JSID-NONTERMINAL"

        await runner_client.post(
            "/jambonz/status",
            json={"callSid": "JSID-NONTERMINAL", "callStatus": "in-progress"},
        )
        assert not worker_in_pool.is_accepting_calls

    async def test_status_with_duration_updates_db(
        self, runner_client, phone_number, worker_in_pool
    ):
        from app.services import call_service

        call_service.create(
            call_sid="JSID-DURATION",
            direction="inbound",
            from_number="+1111",
            to_number="+2222",
            provider="jambonz",
        )
        await runner_client.post(
            "/jambonz/status",
            json={
                "callSid": "JSID-DURATION",
                "callStatus": "completed",
                "duration": 300,
            },
        )
        call = call_service.get_by_sid("JSID-DURATION")
        assert call["duration_seconds"] == 300

    async def test_missing_call_sid_returns_ok(self, runner_client):
        """Missing call_sid should not crash the endpoint."""
        resp = await runner_client.post(
            "/jambonz/status",
            json={"callStatus": "completed"},
        )
        assert resp.status_code == 200

    async def test_empty_payload_returns_ok(self, runner_client):
        resp = await runner_client.post("/jambonz/status", json={})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /jambonz/status → sync_call_request_outcome tests
# ---------------------------------------------------------------------------


def _seed_call_request_for_sync(pg_container, org_id, phone_num="+15005550020"):
    """Create a call_request row via service layer (satisfies bot_id NOT NULL)."""
    import psycopg2
    from app.services import assistant_service, phone_number_service
    from app.services import call_request as cr_svc

    digits = phone_num.lstrip("+")[-10:]
    asst = assistant_service.create(
        name=f"JSync Bot {digits}", system_prompt="t", org_id=org_id
    )
    phone = phone_number_service.create(
        phone_number=phone_num,
        org_id=org_id,
        provider="jambonz",
        provider_credentials={"token": "test-tok"},
    )
    row = cr_svc.create(
        org_id=org_id,
        assistant_id=str(asst["id"]),
        phone_number_id=str(phone["id"]),
        to_number=phone_num,
        priority=5,
    )
    with psycopg2.connect(pg_container) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE call_requests SET status = 'initiated' WHERE id = %s",
                (str(row["id"]),),
            )
    return str(row["id"])


class TestJambonzStatusSync:
    """Verify that terminal status webhooks sync the outcome to call_requests."""

    async def test_terminal_status_syncs_to_call_requests(
        self, runner_client, pg_container, test_org_id
    ):
        import psycopg2
        from app.services import call_service

        call_request_id = _seed_call_request_for_sync(
            pg_container, test_org_id, "+15005550020"
        )

        call_sid = "JSID-SYNC-TERM"
        call_service.create(
            call_sid=call_sid,
            direction="outbound",
            from_number="+15005550020",
            to_number="+14155551234",
            org_id=test_org_id,
            provider="jambonz",
            parent_call_sid=call_request_id,
        )

        await runner_client.post(
            "/jambonz/status",
            json={"callSid": call_sid, "callStatus": "completed", "duration": 75},
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
        assert row[2] == 75

    async def test_mid_call_status_syncs_call_status_only(
        self, runner_client, pg_container, test_org_id, worker_in_pool
    ):
        import psycopg2
        from app.services import call_service

        call_request_id = _seed_call_request_for_sync(
            pg_container, test_org_id, "+15005550021"
        )

        call_sid = "JSID-SYNC-MID"
        call_service.create(
            call_sid=call_sid,
            direction="outbound",
            from_number="+15005550021",
            to_number="+14155551234",
            org_id=test_org_id,
            provider="jambonz",
            parent_call_sid=call_request_id,
        )
        worker_in_pool.current_call_sid = call_sid

        await runner_client.post(
            "/jambonz/status",
            json={"callSid": call_sid, "callStatus": "in-progress"},
        )

        with psycopg2.connect(pg_container) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, call_status FROM call_requests WHERE id = %s",
                    (call_request_id,),
                )
                row = cur.fetchone()
        assert row[0] == "initiated"  # lifecycle unchanged
        assert row[1] == "in-progress"  # provider status updated

    async def test_no_parent_call_sid_does_not_raise(self, runner_client):
        """Inbound calls without parent_call_sid should not cause errors."""
        resp = await runner_client.post(
            "/jambonz/status",
            json={"callSid": "JSID-NO-PARENT", "callStatus": "completed"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Webhook authentication — DAAI-138 (merge conflict: security added
# _verify_jambonz_webhook; main added latency instrumentation to same func)
# ---------------------------------------------------------------------------


class TestJambonzWebhookAuthentication:
    """Verify _verify_jambonz_webhook is enforced on both endpoints.

    Uses _bypass_jambonz_auth=False to disable the autouse bypass fixture
    so the real auth function runs.
    """

    @pytest.fixture(autouse=True)
    def _bypass_jambonz_auth(self):
        """Override the module-level autouse fixture — let real auth run."""
        yield

    @contextmanager
    def _auth_patches(self, secret="test-jambonz-secret"):
        """Set JAMBONZ_WEBHOOK_SECRET for testing."""
        with patch("app.routes.jambonz.JAMBONZ_WEBHOOK_SECRET", secret):
            yield

    async def test_call_missing_auth_returns_403(self, runner_client):
        with self._auth_patches():
            resp = await runner_client.post(
                "/jambonz/call",
                json={
                    "callSid": "AUTH-001",
                    "from": "+14155551234",
                    "to": "+15005550020",
                },
            )
        assert resp.status_code == 403

    async def test_call_wrong_bearer_returns_403(self, runner_client):
        with self._auth_patches():
            resp = await runner_client.post(
                "/jambonz/call",
                headers={"Authorization": "Bearer wrong-secret"},
                json={
                    "callSid": "AUTH-002",
                    "from": "+14155551234",
                    "to": "+15005550020",
                },
            )
        assert resp.status_code == 403

    async def test_call_correct_basic_auth_passes(
        self, runner_client, phone_number, worker_in_pool
    ):
        """Correct Basic Auth password must pass."""
        import base64

        with self._auth_patches(secret="correct-secret"):
            basic = base64.b64encode(b"jambonz:correct-secret").decode()
            resp = await runner_client.post(
                "/jambonz/call",
                headers={"Authorization": f"Basic {basic}"},
                json={
                    "callSid": "AUTH-003",
                    "from": "+14155551234",
                    "to": "+15005550020",
                    "direction": "inbound",
                },
            )
        assert resp.status_code == 200

    async def test_call_empty_secret_returns_503(self, runner_client):
        """Empty JAMBONZ_WEBHOOK_SECRET = 503 (fail-closed)."""
        with self._auth_patches(secret=""):
            resp = await runner_client.post(
                "/jambonz/call",
                json={
                    "callSid": "AUTH-503",
                    "from": "+1",
                    "to": "+2",
                    "direction": "inbound",
                },
            )
        assert resp.status_code == 503

    async def test_status_missing_auth_returns_403(self, runner_client):
        with self._auth_patches():
            resp = await runner_client.post(
                "/jambonz/status",
                json={"callSid": "AUTH-STATUS-001", "callStatus": "completed"},
            )
        assert resp.status_code == 403

    async def test_status_correct_basic_auth_passes(self, runner_client):
        import base64

        with self._auth_patches(secret="correct-secret"):
            basic = base64.b64encode(b"jambonz:correct-secret").decode()
            resp = await runner_client.post(
                "/jambonz/status",
                headers={"Authorization": f"Basic {basic}"},
                json={"callSid": "AUTH-STATUS-002", "callStatus": "completed"},
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Latency metadata — DAAI-217 latency optimisation (merge conflict: main added
# runner_webhook_ms and webhook_completed_at to the jambonz_call response)
# ---------------------------------------------------------------------------


class TestJambonzLatencyMetadata:
    """Verify latency instrumentation fields are present in the listen verb metadata."""

    async def test_listen_verb_has_runner_webhook_ms(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/jambonz/call",
            json={
                "callSid": "LAT-001",
                "from": "+14155551234",
                "to": "+15005550020",
                "direction": "inbound",
            },
        )
        assert resp.status_code == 200
        listen_verb = next(v for v in resp.json() if v.get("verb") == "listen")
        assert "runner_webhook_ms" in listen_verb["metadata"]

    async def test_listen_verb_has_webhook_completed_at(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/jambonz/call",
            json={
                "callSid": "LAT-002",
                "from": "+14155551234",
                "to": "+15005550020",
                "direction": "inbound",
            },
        )
        assert resp.status_code == 200
        listen_verb = next(v for v in resp.json() if v.get("verb") == "listen")
        assert "webhook_completed_at" in listen_verb["metadata"]

    async def test_runner_webhook_ms_is_numeric(
        self, runner_client, phone_number, worker_in_pool
    ):
        resp = await runner_client.post(
            "/jambonz/call",
            json={
                "callSid": "LAT-003",
                "from": "+14155551234",
                "to": "+15005550020",
                "direction": "inbound",
            },
        )
        listen_verb = next(v for v in resp.json() if v.get("verb") == "listen")
        ms = listen_verb["metadata"]["runner_webhook_ms"]
        assert isinstance(ms, (int, float))
        assert ms >= 0
