"""Integration tests for the call service (app/services/call.py).

Tests exercise the service layer directly against a real Postgres container.
Every CRUD function and status-transition path is covered.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_call(overrides=None):
    """Create a call record with sensible defaults. Returns the created dict."""
    from app.services import call_service

    defaults = dict(
        call_sid="CA-SVC-001",
        direction="inbound",
        from_number="+14155551234",
        to_number="+18001234567",
        status="initiated",
        provider="twilio",
    )
    if overrides:
        defaults.update(overrides)
    return call_service.create(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCallServiceCreate:
    def test_create_returns_dict_with_id(self, pg_container):
        record = _create_call()
        assert "id" in record
        assert record["id"] is not None

    def test_create_stores_all_basic_fields(self, pg_container):
        record = _create_call(
            overrides={
                "call_sid": "CA-FIELDS",
                "direction": "inbound",
                "from_number": "+14155551234",
                "to_number": "+18001234567",
                "provider": "jambonz",
            }
        )
        assert record["call_sid"] == "CA-FIELDS"
        assert record["direction"] == "inbound"
        assert record["from_number"] == "+14155551234"
        assert record["to_number"] == "+18001234567"
        assert record["provider"] == "jambonz"

    def test_create_default_status_is_initiated(self, pg_container):
        record = _create_call(overrides={"call_sid": "CA-STATUS-DEF"})
        assert record["status"] == "initiated"

    def test_create_with_explicit_status(self, pg_container):
        record = _create_call(
            overrides={"call_sid": "CA-STATUS-RING", "status": "ringing"}
        )
        assert record["status"] == "ringing"

    def test_create_with_custom_params(self, pg_container):
        record = _create_call(
            overrides={
                "call_sid": "CA-CUSTOM",
                "custom_params": {"customer_id": "C123", "campaign": "summer"},
            }
        )
        assert record["custom_params"]["customer_id"] == "C123"
        assert record["custom_params"]["campaign"] == "summer"

    def test_create_with_worker_info(self, pg_container):
        record = _create_call(
            overrides={
                "call_sid": "CA-WORKER",
                "worker_instance_id": "worker-001",
                "worker_host": "localhost:8765",
            }
        )
        assert record["worker_instance_id"] == "worker-001"
        assert record["worker_host"] == "localhost:8765"

    def test_create_with_explicit_call_id(self, pg_container):
        import uuid

        call_id = str(uuid.uuid4())
        record = _create_call(
            overrides={"call_sid": "CA-EXPLICIT-ID", "call_id": call_id}
        )
        assert str(record["id"]) == call_id

    def test_create_sets_started_at(self, pg_container):
        record = _create_call(overrides={"call_sid": "CA-TIME"})
        assert record["started_at"] is not None

    def test_create_with_provider_metadata(self, pg_container):
        record = _create_call(
            overrides={
                "call_sid": "CA-META",
                "provider_metadata": {"AccountSid": "AC123", "CallStatus": "initiated"},
            }
        )
        assert record["provider_metadata"]["AccountSid"] == "AC123"


class TestCallServiceGetBySid:
    def test_get_by_sid_finds_existing(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-GET-SID"})
        record = call_service.get_by_sid("CA-GET-SID")
        assert record is not None
        assert record["call_sid"] == "CA-GET-SID"

    def test_get_by_sid_returns_none_for_unknown(self, pg_container):
        from app.services import call_service

        assert call_service.get_by_sid("NONEXISTENT-SID") is None


class TestCallServiceGetById:
    def test_get_by_id_finds_existing(self, pg_container):
        from app.services import call_service
        import uuid

        call_id = str(uuid.uuid4())
        _create_call(overrides={"call_sid": "CA-GET-ID", "call_id": call_id})
        record = call_service.get_by_id(call_id)
        assert record is not None
        assert str(record["id"]) == call_id

    def test_get_by_id_returns_none_for_unknown(self, pg_container):
        from app.services import call_service

        assert call_service.get_by_id("00000000-0000-0000-0000-000000000000") is None


class TestCallServiceUpdateStatus:
    def test_update_status_completed_sets_ended_at(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-COMP"})
        updated = call_service.update_status("CA-COMP", status="completed")
        assert updated["status"] == "completed"
        assert updated["ended_at"] is not None

    def test_update_status_completed_with_duration(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-DUR"})
        updated = call_service.update_status(
            "CA-DUR", status="completed", duration_seconds=120
        )
        assert updated["duration_seconds"] == 120

    def test_update_status_in_progress_sets_answered_at(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-INPROG"})
        updated = call_service.update_status("CA-INPROG", status="in-progress")
        assert updated["status"] == "in-progress"
        assert updated["answered_at"] is not None

    def test_update_status_failed_with_error_fields(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-FAIL"})
        updated = call_service.update_status(
            "CA-FAIL",
            status="failed",
            error_code="30001",
            error_message="Connection timeout",
        )
        assert updated["status"] == "failed"
        assert updated["error_code"] == "30001"
        assert updated["error_message"] == "Connection timeout"

    def test_update_status_no_answer(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-NOANSWER"})
        updated = call_service.update_status("CA-NOANSWER", status="no-answer")
        assert updated["status"] == "no-answer"

    def test_update_status_busy(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-BUSY"})
        updated = call_service.update_status("CA-BUSY", status="busy")
        assert updated["status"] == "busy"

    def test_update_status_canceled(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-CANCEL"})
        updated = call_service.update_status("CA-CANCEL", status="canceled")
        assert updated["status"] == "canceled"

    def test_update_status_unknown_sid_returns_none(self, pg_container):
        from app.services import call_service

        result = call_service.update_status("NONEXISTENT", status="completed")
        assert result is None

    def test_update_status_with_recording_url(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-REC"})
        updated = call_service.update_status(
            "CA-REC",
            status="completed",
            recording_url="https://recordings.example.com/CA-REC.mp3",
        )
        assert updated["recording_url"] == "https://recordings.example.com/CA-REC.mp3"


class TestCallServiceUpdateWorkerAssignment:
    def test_updates_worker_and_status(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-ASSIGN"})
        updated = call_service.update_worker_assignment(
            call_sid="CA-ASSIGN",
            worker_instance_id="worker-new",
            worker_host="10.0.0.5:8765",
        )
        assert updated["worker_instance_id"] == "worker-new"
        assert updated["worker_host"] == "10.0.0.5:8765"
        assert updated["status"] == "in-progress"
        assert updated["answered_at"] is not None

    def test_update_worker_unknown_sid_returns_none(self, pg_container):
        from app.services import call_service

        result = call_service.update_worker_assignment(
            call_sid="NONEXISTENT",
            worker_instance_id="w1",
            worker_host="localhost:8765",
        )
        assert result is None


class TestCallServiceUpdateProviderMetadata:
    def test_updates_provider_metadata(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-PMETA"})
        updated = call_service.update_provider_metadata(
            "CA-PMETA",
            provider_metadata={"external_id": "EXT-123", "status": "connected"},
        )
        assert updated["provider_metadata"]["external_id"] == "EXT-123"

    def test_update_provider_metadata_unknown_returns_none(self, pg_container):
        from app.services import call_service

        result = call_service.update_provider_metadata(
            "NONEXISTENT", provider_metadata={}
        )
        assert result is None


class TestCallServiceTranscript:
    def test_add_transcript_message_appends(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-TRANSCRIPT"})
        updated = call_service.add_transcript_message(
            "CA-TRANSCRIPT", "user", "Hello bot!"
        )
        transcript = updated["transcript"]
        assert len(transcript) == 1
        assert transcript[0]["role"] == "user"
        assert transcript[0]["content"] == "Hello bot!"
        assert "timestamp" in transcript[0]

    def test_add_multiple_transcript_messages(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-MULTI-TX"})
        call_service.add_transcript_message("CA-MULTI-TX", "user", "Hi!")
        call_service.add_transcript_message(
            "CA-MULTI-TX", "assistant", "Hello! How can I help?"
        )
        updated = call_service.add_transcript_message(
            "CA-MULTI-TX", "user", "What time is it?"
        )
        transcript = updated["transcript"]
        assert len(transcript) == 3
        assert transcript[0]["role"] == "user"
        assert transcript[1]["role"] == "assistant"
        assert transcript[2]["role"] == "user"

    def test_add_transcript_unknown_sid_returns_none(self, pg_container):
        from app.services import call_service

        result = call_service.add_transcript_message("NONEXISTENT", "user", "test")
        assert result is None


class TestCallServiceSetSummary:
    def test_set_summary(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-SUMMARY"})
        updated = call_service.set_summary(
            "CA-SUMMARY", "Customer asked about pricing."
        )
        assert updated["summary"] == "Customer asked about pricing."

    def test_set_summary_unknown_sid_returns_none(self, pg_container):
        from app.services import call_service

        result = call_service.set_summary("NONEXISTENT", "summary text")
        assert result is None


class TestCallServiceGetMany:
    def test_get_many_empty_returns_empty_list(self, pg_container):
        from app.services import call_service

        result = call_service.get_many()
        assert result == []

    def test_get_many_returns_all_calls(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-MANY-1"})
        _create_call(overrides={"call_sid": "CA-MANY-2"})
        _create_call(overrides={"call_sid": "CA-MANY-3"})
        result = call_service.get_many()
        assert len(result) == 3

    def test_get_many_filter_by_direction(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-DIR-IN", "direction": "inbound"})
        _create_call(overrides={"call_sid": "CA-DIR-OUT", "direction": "outbound"})
        inbound = call_service.get_many(direction="inbound")
        outbound = call_service.get_many(direction="outbound")
        assert len(inbound) == 1
        assert inbound[0]["direction"] == "inbound"
        assert len(outbound) == 1

    def test_get_many_filter_by_status(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-ST-INIT", "status": "initiated"})
        _create_call(overrides={"call_sid": "CA-ST-COMP", "status": "completed"})
        completed = call_service.get_many(status="completed")
        assert len(completed) == 1
        assert completed[0]["status"] == "completed"

    def test_get_many_filter_by_from_number(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-FROM-1", "from_number": "+10000000001"})
        _create_call(overrides={"call_sid": "CA-FROM-2", "from_number": "+10000000002"})
        result = call_service.get_many(from_number="+10000000001")
        assert len(result) == 1

    def test_get_many_limit_respected(self, pg_container):
        from app.services import call_service

        for i in range(5):
            _create_call(overrides={"call_sid": f"CA-LIMIT-{i}"})
        result = call_service.get_many(limit=2)
        assert len(result) == 2

    def test_get_many_offset_respected(self, pg_container):
        from app.services import call_service

        for i in range(3):
            _create_call(overrides={"call_sid": f"CA-OFFSET-{i}"})
        all_calls = call_service.get_many()
        offset_calls = call_service.get_many(offset=1)
        assert len(offset_calls) == len(all_calls) - 1

    def test_get_many_ordered_by_created_at_desc(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-ORD-1"})
        _create_call(overrides={"call_sid": "CA-ORD-2"})
        _create_call(overrides={"call_sid": "CA-ORD-3"})
        result = call_service.get_many()
        # Most recent first
        created_ats = [r["created_at"] for r in result]
        assert created_ats == sorted(created_ats, reverse=True)


# ---------------------------------------------------------------------------
# set_recording_url
# ---------------------------------------------------------------------------


class TestCallServiceSetRecordingUrl:
    def test_set_recording_url_updates_url(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-REC-URL"})
        updated = call_service.set_recording_url(
            "CA-REC-URL", "https://recordings.example.com/CA-REC-URL.mp3"
        )
        assert updated is not None
        assert (
            updated["recording_url"] == "https://recordings.example.com/CA-REC-URL.mp3"
        )

    def test_set_recording_url_returns_full_call_dict(self, pg_container):
        from app.services import call_service

        _create_call(overrides={"call_sid": "CA-REC-DICT"})
        updated = call_service.set_recording_url(
            "CA-REC-DICT", "https://example.com/rec.mp3"
        )
        assert "call_sid" in updated
        assert updated["call_sid"] == "CA-REC-DICT"

    def test_set_recording_url_unknown_sid_returns_none(self, pg_container):
        from app.services import call_service

        result = call_service.set_recording_url(
            "NONEXISTENT-SID", "https://example.com/rec.mp3"
        )
        assert result is None


# ---------------------------------------------------------------------------
# sync_call_request_outcome
# ---------------------------------------------------------------------------


def _seed_call_request_row(pg_container, org_id, status="queued"):
    """Insert a call_requests row via the service layer (satisfies NOT NULL on bot_id)."""
    from app.services import assistant_service, phone_number_service
    from app.services import call_request as call_request_service

    # Create a minimal assistant and phone number for this row
    asst = assistant_service.create(
        name="Sync Test Bot",
        system_prompt="test",
        org_id=org_id,
    )
    phone = phone_number_service.create(
        phone_number="+917022555001",
        org_id=org_id,
        provider="twilio",
        provider_credentials={"account_sid": "AC_sync", "auth_token": "tok"},
    )
    row = call_request_service.create(
        org_id=org_id,
        assistant_id=str(asst["id"]),
        phone_number_id=str(phone["id"]),
        to_number="+917022123456",
        priority=5,
    )
    request_id = str(row["id"])

    # Override to the desired status via SQL if not queued
    if status != "queued":
        import psycopg2

        with psycopg2.connect(pg_container) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE call_requests SET status = %s WHERE id = %s",
                    (status, request_id),
                )
    return request_id


def _get_call_request_row(pg_container, request_id):
    import psycopg2

    with psycopg2.connect(pg_container) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM call_requests WHERE id = %s", (request_id,))
            row = cur.fetchone()
            if row:
                columns = [desc[0] for desc in cur.description]
                return dict(zip(columns, row))
            return None


class TestCallServiceSyncCallRequest:
    async def test_terminal_status_updates_lifecycle_status(
        self, pg_container, test_org_id
    ):
        from app.services import call_service

        request_id = _seed_call_request_row(pg_container, test_org_id)
        await call_service.sync_call_request_outcome(
            call_id=request_id, call_status="completed"
        )
        row = _get_call_request_row(pg_container, request_id)
        assert row["status"] == "completed"
        assert row["call_status"] == "completed"

    async def test_terminal_status_with_duration_stores_seconds_and_minutes(
        self, pg_container, test_org_id
    ):
        from app.services import call_service

        request_id = _seed_call_request_row(pg_container, test_org_id)
        await call_service.sync_call_request_outcome(
            call_id=request_id, call_status="completed", duration_seconds=180
        )
        row = _get_call_request_row(pg_container, request_id)
        assert row["call_duration_seconds"] == 180
        assert round(row["call_duration_minutes"], 2) == 3.0

    async def test_canceled_maps_to_cancelled_double_l(self, pg_container, test_org_id):
        """Twilio sends 'canceled' (one l); call_requests stores 'cancelled' (two l)."""
        from app.services import call_service

        request_id = _seed_call_request_row(pg_container, test_org_id)
        await call_service.sync_call_request_outcome(
            call_id=request_id, call_status="canceled"
        )
        row = _get_call_request_row(pg_container, request_id)
        assert row["status"] == "cancelled"
        assert row["call_status"] == "cancelled"

    async def test_non_terminal_updates_call_status_only(
        self, pg_container, test_org_id
    ):
        """Mid-call status only updates call_status; lifecycle status stays unchanged."""
        from app.services import call_service

        request_id = _seed_call_request_row(pg_container, test_org_id, status="queued")
        await call_service.sync_call_request_outcome(
            call_id=request_id, call_status="in-progress"
        )
        row = _get_call_request_row(pg_container, request_id)
        assert row["status"] == "queued"  # lifecycle unchanged
        assert row["call_status"] == "in-progress"  # provider status updated

    async def test_recording_url_stored(self, pg_container, test_org_id):
        from app.services import call_service

        request_id = _seed_call_request_row(pg_container, test_org_id)
        await call_service.sync_call_request_outcome(
            call_id=request_id,
            call_status="completed",
            recording_url="https://s3.example.com/recording.mp3",
        )
        row = _get_call_request_row(pg_container, request_id)
        assert row["recording_url"] == "https://s3.example.com/recording.mp3"

    async def test_none_recording_url_preserves_existing(
        self, pg_container, test_org_id
    ):
        """When recording_url=None, existing recording_url is not overwritten."""
        from app.services import call_service

        request_id = _seed_call_request_row(pg_container, test_org_id)
        # Set initial URL
        await call_service.sync_call_request_outcome(
            call_id=request_id,
            call_status="completed",
            recording_url="https://original.example.com/rec.mp3",
        )
        # Sync again without URL — should not overwrite
        await call_service.sync_call_request_outcome(
            call_id=request_id,
            call_status="completed",
            recording_url=None,
        )
        row = _get_call_request_row(pg_container, request_id)
        assert row["recording_url"] == "https://original.example.com/rec.mp3"

    async def test_failed_status_stored(self, pg_container, test_org_id):
        from app.services import call_service

        request_id = _seed_call_request_row(pg_container, test_org_id)
        await call_service.sync_call_request_outcome(
            call_id=request_id, call_status="failed"
        )
        row = _get_call_request_row(pg_container, request_id)
        assert row["status"] == "failed"

    async def test_busy_status_stored(self, pg_container, test_org_id):
        from app.services import call_service

        request_id = _seed_call_request_row(pg_container, test_org_id)
        await call_service.sync_call_request_outcome(
            call_id=request_id, call_status="busy"
        )
        row = _get_call_request_row(pg_container, request_id)
        assert row["status"] == "busy"

    async def test_no_answer_status_stored(self, pg_container, test_org_id):
        from app.services import call_service

        request_id = _seed_call_request_row(pg_container, test_org_id)
        await call_service.sync_call_request_outcome(
            call_id=request_id, call_status="no-answer"
        )
        row = _get_call_request_row(pg_container, request_id)
        assert row["status"] == "no-answer"

    async def test_unknown_id_does_not_raise(self, pg_container):
        """sync_call_request_outcome swallows all errors; never raises."""
        from app.services import call_service

        # Should complete without exception even for a non-existent ID
        await call_service.sync_call_request_outcome(
            call_id="00000000-0000-0000-0000-000000000000",
            call_status="completed",
        )
