"""Integration tests for POST/GET /v1/insights/analyse.

Auth: verify_customer_api_key — requests must include X-API-Key header with
a valid per-org API key.  runner_client already sets this header by default.
"""

import uuid

import pytest


ANALYSE_URL = "/insights/analyse"
CONFIG_URL = "/insights/config"


def _config_payload(org_id: str, **kw) -> dict:
    return {"org_id": org_id, "is_default": True, **kw}


async def _create_config(runner_client, org_id: str) -> str:
    r = await runner_client.post(CONFIG_URL, json=_config_payload(org_id))
    assert r.status_code == 201
    return r.json()["id"]


# =============================================================================
# POST /v1/insights/analyse
# =============================================================================


class TestSubmitExternalJob:
    # ── happy paths ───────────────────────────────────────────────────────────

    async def test_default_config_used_when_none_specified(
        self, runner_client, test_api_key, test_org_id
    ):
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            ANALYSE_URL,
            json={"audio_url": "https://example.com/call.mp3"},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert "request_id" in data
        assert data["status"] == "queued"

    async def test_explicit_config_id(self, runner_client, test_api_key, test_org_id):
        config_id = await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            ANALYSE_URL,
            json={
                "audio_url": "https://example.com/call.wav",
                "insights_config_id": config_id,
            },
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 202

    async def test_callback_url_stored(
        self, runner_client, test_api_key, test_org_id, pg_container
    ):
        import psycopg2
        import psycopg2.extras

        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            ANALYSE_URL,
            json={
                "audio_url": "https://example.com/call.mp3",
                "callback_url": "https://hooks.example.com/notify",
            },
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 202
        request_id = resp.json()["request_id"]

        # Look up the call_analysis via the insights_jobs row
        with psycopg2.connect(
            pg_container, cursor_factory=psycopg2.extras.RealDictCursor
        ) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT ca.callback_url FROM call_analysis ca
                       JOIN insights_jobs j ON j.call_analysis_id = ca.id
                       WHERE j.id = %s""",
                    (str(request_id),),
                )
                row = cur.fetchone()
        assert row["callback_url"] == "https://hooks.example.com/notify"

    async def test_custom_file_name_stored(
        self, runner_client, test_api_key, test_org_id, pg_container
    ):
        import psycopg2
        import psycopg2.extras

        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            ANALYSE_URL,
            json={
                "audio_url": "https://example.com/call.mp3",
                "file_name": "my_custom_recording.mp3",
            },
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 202
        request_id = resp.json()["request_id"]

        with psycopg2.connect(
            pg_container, cursor_factory=psycopg2.extras.RealDictCursor
        ) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT ca.file_name FROM call_analysis ca
                       JOIN insights_jobs j ON j.call_analysis_id = ca.id
                       WHERE j.id = %s""",
                    (str(request_id),),
                )
                row = cur.fetchone()
        assert row["file_name"] == "my_custom_recording.mp3"

    async def test_priority_1_accepted(self, runner_client, test_api_key, test_org_id):
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            ANALYSE_URL,
            json={"audio_url": "https://example.com/call.mp3", "priority": 1},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 202

    async def test_priority_100_accepted(
        self, runner_client, test_api_key, test_org_id
    ):
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            ANALYSE_URL,
            json={"audio_url": "https://example.com/call.mp3", "priority": 100},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 202

    # ── URL scheme → audio_source_type mapping ────────────────────────────────

    @pytest.mark.parametrize(
        "url, expected_type",
        [
            ("https://cdn.example.com/call.mp3", "public_url"),
            ("http://cdn.example.com/call.mp3", "public_url"),
            ("s3://my-bucket/recordings/call.wav", "s3"),
            ("r2://my-bucket/recordings/call.wav", "r2"),
            ("gs://my-bucket/recordings/call.wav", "gcs"),
            ("gcs://my-bucket/recordings/call.wav", "gcs"),
            ("az://my-container/recordings/call.wav", "azure"),
            ("azure://my-container/recordings/call.wav", "azure"),
            ("supabase://my-bucket/recordings/call.wav", "supabase"),
            ("data:audio/wav;base64,UklGRiQAAABXQVZF", "base64"),
        ],
    )
    async def test_audio_source_type_detection(
        self, runner_client, test_api_key, test_org_id, pg_container, url, expected_type
    ):
        import psycopg2
        import psycopg2.extras

        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            ANALYSE_URL,
            json={"audio_url": url},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 202, resp.text
        request_id = resp.json()["request_id"]

        with psycopg2.connect(
            pg_container, cursor_factory=psycopg2.extras.RealDictCursor
        ) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT ca.audio_source_type FROM call_analysis ca
                       JOIN insights_jobs j ON j.call_analysis_id = ca.id
                       WHERE j.id = %s""",
                    (str(request_id),),
                )
                row = cur.fetchone()
        assert row["audio_source_type"] == expected_type

    # ── file name extraction ──────────────────────────────────────────────────

    async def test_file_name_extracted_from_url(
        self, runner_client, test_api_key, test_org_id, pg_container
    ):
        import psycopg2
        import psycopg2.extras

        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            ANALYSE_URL,
            json={"audio_url": "https://example.com/calls/recording.mp3"},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 202
        request_id = resp.json()["request_id"]

        with psycopg2.connect(
            pg_container, cursor_factory=psycopg2.extras.RealDictCursor
        ) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT ca.file_name FROM call_analysis ca
                       JOIN insights_jobs j ON j.call_analysis_id = ca.id
                       WHERE j.id = %s""",
                    (str(request_id),),
                )
                row = cur.fetchone()
        assert row["file_name"] == "recording.mp3"

    async def test_malformed_url_defaults_file_name(
        self, runner_client, test_api_key, test_org_id, pg_container
    ):
        import psycopg2
        import psycopg2.extras

        await _create_config(runner_client, test_org_id)
        # s3:// URL with no path component beyond the bucket
        resp = await runner_client.post(
            ANALYSE_URL,
            json={"audio_url": "s3://bucket"},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 202
        request_id = resp.json()["request_id"]

        with psycopg2.connect(
            pg_container, cursor_factory=psycopg2.extras.RealDictCursor
        ) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT ca.file_name FROM call_analysis ca
                       JOIN insights_jobs j ON j.call_analysis_id = ca.id
                       WHERE j.id = %s""",
                    (str(request_id),),
                )
                row = cur.fetchone()
        assert row["file_name"] == "unknown.wav"

    # ── org isolation behavior ────────────────────────────────────────────────

    async def test_config_from_different_org_accepted(
        self, runner_client, test_api_key
    ):
        """insights_config_id is not validated against org — cross-org config_id works."""
        other_org = str(uuid.uuid4())
        create = await runner_client.post(CONFIG_URL, json={"org_id": other_org})
        other_config_id = create.json()["id"]

        resp = await runner_client.post(
            ANALYSE_URL,
            json={
                "audio_url": "https://example.com/call.mp3",
                "insights_config_id": other_config_id,
            },
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 202

    # ── error cases ───────────────────────────────────────────────────────────

    async def test_no_active_config_returns_422(self, runner_client, test_api_key):
        """No config seeded for this fresh org → 422."""
        resp = await runner_client.post(
            ANALYSE_URL,
            json={"audio_url": "https://example.com/call.mp3"},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 422
        assert "insights_config" in resp.json()["detail"].lower()

    async def test_nonexistent_config_id_returns_422(self, runner_client, test_api_key):
        """FK violation when config_id doesn't exist → handle_db_errors → 422."""
        resp = await runner_client.post(
            ANALYSE_URL,
            json={
                "audio_url": "https://example.com/call.mp3",
                "insights_config_id": str(uuid.uuid4()),
            },
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 422

    async def test_priority_zero_returns_422(
        self, runner_client, test_api_key, test_org_id
    ):
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            ANALYSE_URL,
            json={"audio_url": "https://example.com/call.mp3", "priority": 0},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 422

    async def test_priority_101_returns_422(
        self, runner_client, test_api_key, test_org_id
    ):
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            ANALYSE_URL,
            json={"audio_url": "https://example.com/call.mp3", "priority": 101},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 422

    async def test_missing_audio_url_returns_422(self, runner_client, test_api_key):
        resp = await runner_client.post(
            ANALYSE_URL,
            json={},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 422

    async def test_missing_api_key_returns_401(self, runner_client):
        resp = await runner_client.post(
            ANALYSE_URL,
            json={"audio_url": "https://example.com/call.mp3"},
            headers={"X-API-Key": ""},
        )
        assert resp.status_code == 401

    async def test_invalid_api_key_returns_403(self, runner_client):
        resp = await runner_client.post(
            ANALYSE_URL,
            json={"audio_url": "https://example.com/call.mp3"},
            headers={"X-API-Key": "inv_thiskeyisnotvalid"},
        )
        assert resp.status_code == 403


# =============================================================================
# GET /v1/insights/analyse
# =============================================================================


class TestGetAnalysis:
    async def _submit(self, runner_client, test_api_key, test_org_id) -> dict:
        """Helper: create a config and submit one analysis job, return response body."""
        config_id = await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            ANALYSE_URL,
            json={
                "audio_url": "https://example.com/call.mp3",
                "insights_config_id": config_id,
            },
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 202
        return resp.json()

    async def test_get_by_request_id(self, runner_client, test_api_key, test_org_id):
        """External upload: request_id = insights_jobs.id — should resolve."""
        job = await self._submit(runner_client, test_api_key, test_org_id)
        resp = await runner_client.get(
            ANALYSE_URL,
            params={"request_id": job["request_id"]},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 200

    async def test_get_by_parent_call_request_id(
        self, runner_client, test_api_key, test_org_id, pg_container
    ):
        """Call-originated: request_id = parent_call_request_id — should resolve."""
        import psycopg2
        import psycopg2.extras

        job = await self._submit(runner_client, test_api_key, test_org_id)
        fake_call_request_id = str(uuid.uuid4())

        # Manually set parent_call_request_id on the insights_jobs row
        with psycopg2.connect(pg_container) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE insights_jobs SET parent_call_request_id = %s WHERE id = %s",
                    (fake_call_request_id, str(job["request_id"])),
                )

        resp = await runner_client.get(
            ANALYSE_URL,
            params={"request_id": fake_call_request_id},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 200

    async def test_missing_request_id_returns_422(self, runner_client, test_api_key):
        resp = await runner_client.get(
            ANALYSE_URL,
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 422

    async def test_unknown_request_id_returns_404(self, runner_client, test_api_key):
        resp = await runner_client.get(
            ANALYSE_URL,
            params={"request_id": str(uuid.uuid4())},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 404

    async def test_request_id_different_org_returns_404(
        self, runner_client, test_api_key, test_org_id, pg_container
    ):
        """request_id that exists but belongs to a different org → 404 (org-scoped SQL)."""
        import psycopg2
        import psycopg2.extras

        other_org_id = None
        analysis_id = None
        job_id = None
        with psycopg2.connect(
            pg_container, cursor_factory=psycopg2.extras.RealDictCursor
        ) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO organizations (name, org_type, is_active) VALUES ('Other Org A', 'demo', TRUE) RETURNING id"
                )
                other_org_id = str(cur.fetchone()["id"])
                cur.execute(
                    "INSERT INTO insights_config (org_id, name) VALUES (%s, 'other-config-a') RETURNING id",
                    (other_org_id,),
                )
                config_id = str(cur.fetchone()["id"])
                cur.execute(
                    """INSERT INTO call_analysis (org_id, insights_config_id, audio_url, file_name, audio_source_type)
                       VALUES (%s, %s, 'https://example.com/other.mp3', 'other.mp3', 'public_url') RETURNING id""",
                    (other_org_id, config_id),
                )
                analysis_id = str(cur.fetchone()["id"])
                cur.execute(
                    "INSERT INTO insights_jobs (call_analysis_id) VALUES (%s) RETURNING id",
                    (analysis_id,),
                )
                job_id = str(cur.fetchone()["id"])

        try:
            resp = await runner_client.get(
                ANALYSE_URL,
                params={"request_id": job_id},
                headers={"X-API-Key": test_api_key},
            )
            assert resp.status_code == 404
        finally:
            with psycopg2.connect(pg_container) as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    if job_id:
                        cur.execute(
                            "DELETE FROM insights_jobs WHERE id = %s", (job_id,)
                        )
                    if analysis_id:
                        cur.execute(
                            "DELETE FROM call_analysis WHERE id = %s", (analysis_id,)
                        )
                    if other_org_id:
                        cur.execute(
                            "DELETE FROM insights_config WHERE org_id = %s",
                            (other_org_id,),
                        )
                        cur.execute(
                            "DELETE FROM organizations WHERE id = %s", (other_org_id,)
                        )

    async def test_invalid_uuid_request_id_returns_422(
        self, runner_client, test_api_key
    ):
        resp = await runner_client.get(
            ANALYSE_URL,
            params={"request_id": "not-a-uuid"},
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 422

    async def test_missing_api_key_returns_401(self, runner_client):
        resp = await runner_client.get(
            ANALYSE_URL,
            params={"request_id": str(uuid.uuid4())},
            headers={"X-API-Key": ""},
        )
        assert resp.status_code == 401


# =============================================================================
# POST /v1/insights/analyse/upload
# =============================================================================

UPLOAD_URL = "/insights/analyse/upload"

# ---------------------------------------------------------------------------
# Minimal valid audio byte payloads (just enough bytes for magic-byte checks)
# ---------------------------------------------------------------------------

# WAV: "RIFF" at 0, "WAVE" at 8
_WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt "
# MP3 with ID3 tag
_MP3_ID3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 100
# MP3 without ID3 tag — raw MPEG sync word (0xFF 0xFB = MPEG1, Layer3, 128kbps)
_MP3_SYNC_BYTES = b"\xff\xfb\x90\x00" + b"\x00" * 100
# MP3 without ID3 — uncommon sync variant (0xFF 0xF3); covered by broad mask fix
_MP3_UNCOMMON_SYNC_BYTES = b"\xff\xf3\x90\x00" + b"\x00" * 100
# MP3 — another valid MPEG sync (0xFF 0xFA = MPEG1, Layer3, free format)
_MP3_ANOTHER_SYNC_BYTES = b"\xff\xfa\x90\x00" + b"\x00" * 100
# OGG
_OGG_BYTES = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00" + b"\x00" * 100
# FLAC
_FLAC_BYTES = b"fLaC\x00\x00\x00\x22" + b"\x00" * 100
# MP4 / M4A: "ftyp" at bytes 4-7
_MP4_BYTES = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100


def _make_file(name: str, content: bytes, mime: str) -> dict:
    """Return the httpx files dict for a multipart POST."""
    return {"file": (name, content, mime)}


class TestUploadHappyPaths:
    """202 + correct response shape for every supported audio format."""

    async def _upload(self, runner_client, test_org_id, name, content, mime, mocker):
        await _create_config(runner_client, test_org_id)
        mocker.patch(
            "app.routes.insights_analyse.upload_file_to_s3",
            return_value=f"s3://test-bucket/file_uploads/{test_org_id}/job-id.wav",
        )
        return await runner_client.post(
            UPLOAD_URL,
            files=_make_file(name, content, mime),
        )

    @pytest.mark.parametrize(
        "name, content, mime",
        [
            ("call.wav", _WAV_BYTES, "audio/wav"),
            ("call.wav", _WAV_BYTES, "audio/x-wav"),  # Gap 3: x- alias
            ("call.mp3", _MP3_ID3_BYTES, "audio/mpeg"),
            ("call.mp3", _MP3_SYNC_BYTES, "audio/mpeg"),
            ("call.mp3", _MP3_UNCOMMON_SYNC_BYTES, "audio/mpeg"),
            ("call.mp3", _MP3_ANOTHER_SYNC_BYTES, "audio/mpeg"),
            ("call.ogg", _OGG_BYTES, "audio/ogg"),
            ("call.flac", _FLAC_BYTES, "audio/flac"),
            ("call.flac", _FLAC_BYTES, "audio/x-flac"),  # Gap 3: x- alias
            ("call.mp4", _MP4_BYTES, "audio/mp4"),
            ("call.mp4", _MP4_BYTES, "video/mp4"),  # Gap 3: video/mp4 alias
            ("call.m4a", _MP4_BYTES, "audio/m4a"),
            ("call.m4a", _MP4_BYTES, "audio/x-m4a"),  # Gap 3: x- alias
        ],
    )
    async def test_returns_202_for_supported_format(
        self, runner_client, test_org_id, mocker, name, content, mime
    ):
        resp = await self._upload(
            runner_client, test_org_id, name, content, mime, mocker
        )
        assert resp.status_code == 202, resp.text
        data = resp.json()
        assert "request_id" in data
        assert data["status"] == "queued"


class TestUploadDbVerification:
    """After a successful upload the correct rows are written to the database."""

    async def _upload_and_query(
        self,
        runner_client,
        test_org_id,
        pg_container,
        mocker,
        files=None,
        data=None,
        s3_uri=None,
    ):
        import psycopg2
        import psycopg2.extras

        await _create_config(runner_client, test_org_id)
        captured_s3_uri = (
            s3_uri or f"s3://test-bucket/file_uploads/{test_org_id}/job-id.wav"
        )
        mocker.patch(
            "app.routes.insights_analyse.upload_file_to_s3",
            return_value=captured_s3_uri,
        )
        files = files or _make_file("call.wav", _WAV_BYTES, "audio/wav")
        resp = await runner_client.post(UPLOAD_URL, files=files, data=data or {})
        assert resp.status_code == 202, resp.text
        request_id = resp.json()["request_id"]

        with psycopg2.connect(
            pg_container, cursor_factory=psycopg2.extras.RealDictCursor
        ) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT ca.audio_source_type, ca.audio_url, ca.file_name,
                              ca.callback_url, ca.additional_data,
                              j.id AS job_id, j.priority, j.org_id AS job_org_id
                       FROM call_analysis ca
                       JOIN insights_jobs j ON j.call_analysis_id = ca.id
                       WHERE j.id = %s""",
                    (str(request_id),),
                )
                row = cur.fetchone()
        return request_id, dict(row)

    async def test_audio_source_type_is_s3(
        self, runner_client, test_org_id, pg_container, mocker
    ):
        _, row = await self._upload_and_query(
            runner_client, test_org_id, pg_container, mocker
        )
        assert row["audio_source_type"] == "s3"

    async def test_audio_url_is_s3_uri(
        self, runner_client, test_org_id, pg_container, mocker
    ):
        _, row = await self._upload_and_query(
            runner_client, test_org_id, pg_container, mocker
        )
        assert row["audio_url"].startswith("s3://")

    async def test_request_id_matches_job_id_and_s3_key(
        self, runner_client, test_org_id, pg_container, mocker
    ):
        """The pre-generated job_id is used as both insights_jobs.id and the S3 key filename."""
        import psycopg2  # noqa: F401 — used via pg_container connection string

        await _create_config(runner_client, test_org_id)
        captured: list[str] = []

        def _fake_upload(file_obj, s3_key, content_type, bucket=None):
            captured.append(s3_key)
            return f"s3://test-bucket/{s3_key}"

        mocker.patch(
            "app.routes.insights_analyse.upload_file_to_s3", side_effect=_fake_upload
        )
        resp = await runner_client.post(
            UPLOAD_URL, files=_make_file("call.wav", _WAV_BYTES, "audio/wav")
        )
        assert resp.status_code == 202, resp.text
        request_id = resp.json()["request_id"]
        # S3 key must contain the same UUID that was returned as request_id
        assert len(captured) == 1
        assert str(request_id) in captured[0]

    async def test_file_name_stored_from_upload_filename(
        self, runner_client, test_org_id, pg_container, mocker
    ):
        _, row = await self._upload_and_query(
            runner_client,
            test_org_id,
            pg_container,
            mocker,
            files=_make_file("my_recording.wav", _WAV_BYTES, "audio/wav"),
        )
        assert row["file_name"] == "my_recording.wav"

    async def test_callback_url_stored(
        self, runner_client, test_org_id, pg_container, mocker
    ):
        _, row = await self._upload_and_query(
            runner_client,
            test_org_id,
            pg_container,
            mocker,
            data={"callback_url": "https://hooks.example.com/cb"},
        )
        assert row["callback_url"] == "https://hooks.example.com/cb"

    async def test_priority_stored(
        self, runner_client, test_org_id, pg_container, mocker
    ):
        _, row = await self._upload_and_query(
            runner_client,
            test_org_id,
            pg_container,
            mocker,
            data={"priority": "42"},
        )
        assert row["priority"] == 42

    async def test_additional_data_stored(
        self, runner_client, test_org_id, pg_container, mocker
    ):
        import json as _json

        _, row = await self._upload_and_query(
            runner_client,
            test_org_id,
            pg_container,
            mocker,
            data={"additional_data": '{"customer_id": "cust_99"}'},
        )
        stored = row["additional_data"]
        if isinstance(stored, str):
            stored = _json.loads(stored)
        assert stored.get("customer_id") == "cust_99"

    async def test_explicit_insights_config_id_stored(
        self, runner_client, test_org_id, pg_container, mocker
    ):
        import psycopg2
        import psycopg2.extras

        config_id = await _create_config(runner_client, test_org_id)
        mocker.patch(
            "app.routes.insights_analyse.upload_file_to_s3",
            return_value=f"s3://test-bucket/file_uploads/{test_org_id}/job-id.wav",
        )
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("call.wav", _WAV_BYTES, "audio/wav"),
            data={"insights_config_id": config_id},
        )
        assert resp.status_code == 202
        request_id = resp.json()["request_id"]

        with psycopg2.connect(
            pg_container, cursor_factory=psycopg2.extras.RealDictCursor
        ) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT ca.insights_config_id FROM call_analysis ca
                       JOIN insights_jobs j ON j.call_analysis_id = ca.id
                       WHERE j.id = %s""",
                    (str(request_id),),
                )
                row = cur.fetchone()
        assert str(row["insights_config_id"]) == config_id

    async def test_windows_filename_sanitized(
        self, runner_client, test_org_id, pg_container, mocker
    ):
        """Bug E regression: Windows path separators must be stripped on Linux."""
        _, row = await self._upload_and_query(
            runner_client,
            test_org_id,
            pg_container,
            mocker,
            files=_make_file(r"C:\Users\vansh\call.wav", _WAV_BYTES, "audio/wav"),
        )
        assert "\\" not in row["file_name"]
        assert "Users" not in row["file_name"]
        assert row["file_name"] == "call.wav"


class TestUploadValidationErrors:
    """400-range errors from the validation layer (no S3 or DB interaction)."""

    async def test_unsupported_mime_type_returns_415(self, runner_client, test_org_id):
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("document.pdf", b"%PDF-1.4", "application/pdf"),
        )
        assert resp.status_code == 415
        assert "application/pdf" in resp.json()["detail"]

    async def test_spoofed_content_type_rejected_by_magic_bytes(
        self, runner_client, test_org_id
    ):
        """Content-Type claims audio/wav but bytes are PDF — magic check must reject it."""
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("fake.wav", b"%PDF-1.4 fake content", "audio/wav"),
        )
        assert resp.status_code == 415

    async def test_file_too_large_returns_413(self, runner_client, test_org_id, mocker):
        await _create_config(runner_client, test_org_id)
        # Temporarily set the limit to 0 so any real file exceeds it
        mocker.patch("app.routes.insights_analyse.RECORDING_UPLOAD_MAX_FILE_SIZE_MB", 0)
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("call.wav", _WAV_BYTES, "audio/wav"),
        )
        assert resp.status_code == 413
        assert "MB limit" in resp.json()["detail"]

    async def test_invalid_insights_config_id_uuid_returns_422(
        self, runner_client, test_org_id
    ):
        """Bug C regression: malformed UUID must return 422, not 500."""
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("call.wav", _WAV_BYTES, "audio/wav"),
            data={"insights_config_id": "not-a-valid-uuid"},
        )
        assert resp.status_code == 422
        assert "uuid" in resp.json()["detail"].lower()

    async def test_invalid_additional_data_json_returns_422(
        self, runner_client, test_org_id, mocker
    ):
        await _create_config(runner_client, test_org_id)
        mocker.patch(
            "app.routes.insights_analyse.upload_file_to_s3",
            return_value=f"s3://test-bucket/file_uploads/{test_org_id}/job-id.wav",
        )
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("call.wav", _WAV_BYTES, "audio/wav"),
            data={"additional_data": "not-valid-json{"},
        )
        assert resp.status_code == 422
        assert "additional_data" in resp.json()["detail"].lower()

    async def test_no_default_config_returns_422(self, runner_client):
        """No insights_config seeded → 422 (same as JSON path)."""
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("call.wav", _WAV_BYTES, "audio/wav"),
        )
        assert resp.status_code == 422
        assert "insights_config" in resp.json()["detail"].lower()

    async def test_priority_zero_returns_422(self, runner_client, test_org_id):
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("call.wav", _WAV_BYTES, "audio/wav"),
            data={"priority": "0"},
        )
        assert resp.status_code == 422

    async def test_priority_101_returns_422(self, runner_client, test_org_id):
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("call.wav", _WAV_BYTES, "audio/wav"),
            data={"priority": "101"},
        )
        assert resp.status_code == 422

    async def test_missing_file_field_returns_422(self, runner_client, test_org_id):
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(UPLOAD_URL)
        assert resp.status_code == 422

    async def test_missing_api_key_returns_401(self, runner_client, test_org_id):
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("call.wav", _WAV_BYTES, "audio/wav"),
            headers={"X-API-Key": ""},
        )
        assert resp.status_code == 401

    async def test_invalid_api_key_returns_403(self, runner_client, test_org_id):
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("call.wav", _WAV_BYTES, "audio/wav"),
            headers={"X-API-Key": "inv_thiskeyisnotvalid"},
        )
        assert resp.status_code == 403

    async def test_empty_file_returns_415(self, runner_client, test_org_id):
        """Gap 2: zero-byte file has no magic bytes → 415, not 500."""
        await _create_config(runner_client, test_org_id)
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("empty.mp3", b"", "audio/mpeg"),
        )
        assert resp.status_code == 415

    async def test_additional_data_json_array_returns_422(
        self, runner_client, test_org_id, mocker
    ):
        """Gap 1: valid JSON but not an object (array) → 422 'must be a JSON object'."""
        await _create_config(runner_client, test_org_id)
        mocker.patch(
            "app.routes.insights_analyse.upload_file_to_s3",
            return_value=f"s3://test-bucket/file_uploads/{test_org_id}/job-id.wav",
        )
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("call.wav", _WAV_BYTES, "audio/wav"),
            data={"additional_data": "[1, 2, 3]"},
        )
        assert resp.status_code == 422
        assert "JSON object" in resp.json()["detail"]


class TestUploadS3Failure:
    """S3 upload failure → 500 + no DB rows created."""

    async def test_s3_failure_returns_500(self, runner_client, test_org_id, mocker):
        await _create_config(runner_client, test_org_id)
        mocker.patch(
            "app.routes.insights_analyse.upload_file_to_s3",
            side_effect=Exception("S3 connection refused"),
        )
        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("call.wav", _WAV_BYTES, "audio/wav"),
        )
        assert resp.status_code == 500
        # Bug F regression: internal S3 detail must not be leaked
        assert "S3 connection refused" not in resp.json()["detail"]
        assert "Internal error" in resp.json()["detail"]

    async def test_s3_failure_creates_no_db_rows(
        self, runner_client, test_org_id, pg_container, mocker
    ):
        import psycopg2

        await _create_config(runner_client, test_org_id)
        mocker.patch(
            "app.routes.insights_analyse.upload_file_to_s3",
            side_effect=Exception("S3 timeout"),
        )
        await runner_client.post(
            UPLOAD_URL,
            files=_make_file("call.wav", _WAV_BYTES, "audio/wav"),
        )

        with psycopg2.connect(pg_container) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM call_analysis")
                assert cur.fetchone()[0] == 0
                cur.execute("SELECT COUNT(*) FROM insights_jobs")
                assert cur.fetchone()[0] == 0


class TestUploadOrphanCleanup:
    """Bug B regression: S3 object is deleted when the DB insert fails after upload."""

    async def test_db_failure_after_s3_success_triggers_s3_delete(
        self, runner_client, test_org_id, mocker
    ):
        """If _submit_job raises after upload_file_to_s3 succeeds, the S3 object
        must be deleted via delete_object and a 500 must be returned."""
        await _create_config(runner_client, test_org_id)

        mocker.patch(
            "app.routes.insights_analyse.upload_file_to_s3",
            return_value=f"s3://test-bucket/file_uploads/{test_org_id}/job-uuid.wav",
        )
        mock_s3 = mocker.MagicMock()
        mocker.patch("app.routes.insights_analyse._get_s3_client", return_value=mock_s3)
        mocker.patch(
            "app.routes.insights_analyse._submit_job",
            side_effect=Exception("DB constraint violation"),
        )

        resp = await runner_client.post(
            UPLOAD_URL,
            files=_make_file("call.wav", _WAV_BYTES, "audio/wav"),
        )

        assert resp.status_code == 500
        # delete_object must have been called once with the correct S3 key
        mock_s3.delete_object.assert_called_once()
        call_kwargs = mock_s3.delete_object.call_args
        assert "file_uploads" in str(call_kwargs)
