"""
POST /v1/insights/analyse        — queue an external audio URL for full insight analysis.
POST /v1/insights/analyse/upload — upload a call recording file directly (multipart).
GET  /v1/insights/analyse        — fetch analysis result by request_id.

For the JSON path, call_id is NULL (no calls row created). audio_source_type is
auto-detected from the URL scheme. org_id is derived from the authenticated API key,
never the request body.

For the upload path, the file is validated, streamed to S3, and stored with
audio_source_type='s3'. The worker pipeline presigns the s3:// URI and sends it
to Deepgram — zero worker changes required.

Supported upload formats: mp3, mp4, wav, ogg, flac, m4a
Default max size: 25 MB (hard ceiling: 500 MB, configurable via RECORDING_UPLOAD_MAX_FILE_SIZE_MB)

S3 key layout for uploaded files:
    {RECORDING_UPLOAD_S3_PREFIX}/{org_id}/{job_id}.{ext}
    e.g.  file_uploads/org-uuid/job-uuid.wav
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from loguru import logger
from pydantic import BaseModel, Field
from pydantic import field_validator

from psycopg2.extras import Json as psycopg2_json

from app.config import (
    RECORDING_UPLOAD_ALLOWED_MIME_TYPES,
    RECORDING_UPLOAD_MAX_FILE_SIZE_MB,
    RECORDING_UPLOAD_S3_PREFIX,
    S3_BUCKET_NAME,
)
from app.core.auth import verify_customer_api_key
from app.core.database import get_cursor
from app.services.s3_service import _get_s3_client, upload_file_to_s3
from app.utils.exceptions import handle_db_errors

router = APIRouter(prefix="/insights/analyse", tags=["Insights"])

_API_SOURCE_EXTERNAL = "external"
_ADDITIONAL_DATA_MAX_BYTES = 65536  # 64 KB

# Magic-byte signatures for non-MP3 audio formats.
# MP3 sync words are checked separately in _is_valid_audio_magic because the
# valid MPEG sync range is wider than a fixed byte literal can express.
# Each entry: (offset, bytes_to_match)
_AUDIO_MAGIC_SIGNATURES: list[tuple[int, bytes]] = [
    (0, b"RIFF"),  # WAV  — requires bytes 8-11 == b"WAVE" (checked below)
    (0, b"ID3"),  # MP3  with ID3 tag
    (0, b"OggS"),  # OGG
    (0, b"fLaC"),  # FLAC
    (4, b"ftyp"),  # MP4 / M4A (ISO Base Media File Format)
]
_MAGIC_PEEK_BYTES = 12  # enough to cover all checks above


# ── helpers ───────────────────────────────────────────────────────────────────


def _file_name_from_url(url: str) -> str:
    try:
        path = urlparse(url).path
        return path.rsplit("/", 1)[-1] or "unknown.wav"
    except Exception:
        return "unknown.wav"


def _detect_audio_source_type(url: str) -> str:
    if url.startswith("r2://"):
        return "r2"
    if url.startswith("s3://"):
        return "s3"
    if url.startswith("gs://") or url.startswith("gcs://"):
        return "gcs"
    if url.startswith("az://") or url.startswith("azure://"):
        return "azure"
    if url.startswith("supabase://"):
        return "supabase"
    if url.startswith("data:audio"):
        return "base64"
    return "public_url"


def _sanitize_filename(raw: str | None) -> str:
    """Strip directory components (both Unix and Windows) and unsafe characters."""
    # Normalise Windows backslash paths before os.path.basename so that
    # "C:\\Users\\vansh\\call.wav" correctly yields "call.wav" on Linux.
    name = os.path.basename((raw or "upload.bin").replace("\\", "/")) or "upload.bin"
    # Allow only safe characters; collapse everything else to underscore
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    return safe[:255] or "upload.bin"


def _is_valid_audio_magic(header: bytes) -> bool:
    """Return True if header bytes match any known audio magic signature.

    MP3 sync words are checked first with the full MPEG sync mask
    (0xFF + upper-3-bits of second byte set) to handle the complete range
    of valid MPEG frame headers, not just a handful of literal values.
    """
    # Broad MPEG audio sync check — covers all valid MP3 frame headers
    # including files without ID3 tags and all MPEG layer/bitrate combinations.
    if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
        return True

    for offset, signature in _AUDIO_MAGIC_SIGNATURES:
        end = offset + len(signature)
        if len(header) >= end and header[offset:end] == signature:
            # Extra check for WAV: bytes 8-11 must be b"WAVE"
            if signature == b"RIFF":
                return len(header) >= 12 and header[8:12] == b"WAVE"
            return True

    return False


def _validate_and_upload_recording(
    file: UploadFile, org_id: str, job_id: str
) -> tuple[str, str]:
    """Validate a recording upload and stream it to S3.

    Sync function — call via asyncio.to_thread() from async handlers.

    S3 key: {RECORDING_UPLOAD_S3_PREFIX}/{org_id}/{job_id}.{ext}

    Returns:
        (s3_uri, s3_key) — s3_key is returned separately so the caller can
        attempt a best-effort delete if the subsequent DB insert fails (Bug B fix).

    Raises HTTPException (415 / 413 / 500) on failure.
    """
    # ── 1. MIME type allowlist ────────────────────────────────────────────────
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in RECORDING_UPLOAD_ALLOWED_MIME_TYPES:
        allowed = ", ".join(RECORDING_UPLOAD_ALLOWED_MIME_TYPES)
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported file type '{content_type}'. Allowed types: {allowed}"
            ),
        )

    # ── 2. Magic-byte audio validation ────────────────────────────────────────
    header = file.file.read(_MAGIC_PEEK_BYTES)
    file.file.seek(0)  # SpooledTemporaryFile supports seek — no full-file buffer
    if not _is_valid_audio_magic(header):
        raise HTTPException(
            status_code=415,
            detail=(
                "File content does not match a supported audio format "
                "(mp3, mp4, wav, ogg, flac, m4a)."
            ),
        )

    # ── 3. Size check (seek-based — no RAM buffer) ────────────────────────────
    max_bytes = RECORDING_UPLOAD_MAX_FILE_SIZE_MB * 1024 * 1024
    file.file.seek(0, 2)  # SEEK_END
    file_size = file.file.tell()
    file.file.seek(0)
    if file_size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File size ({file_size / (1024 * 1024):.1f} MB) exceeds "
                f"the {RECORDING_UPLOAD_MAX_FILE_SIZE_MB} MB limit."
            ),
        )

    # ── 4. Build S3 key ───────────────────────────────────────────────────────
    safe_name = _sanitize_filename(file.filename)
    ext = safe_name.rsplit(".", 1)[-1] if "." in safe_name else "bin"
    s3_key = f"{RECORDING_UPLOAD_S3_PREFIX}/{org_id}/{job_id}.{ext}"

    # ── 5. Stream to S3 (no full-file buffer) ─────────────────────────────────
    try:
        s3_uri = upload_file_to_s3(
            file_obj=file.file,
            s3_key=s3_key,
            content_type=content_type,
            bucket=S3_BUCKET_NAME,
        )
    except Exception as exc:
        # Log the full error internally; return a generic message to the caller
        # so bucket names and AWS internals are not exposed (Bug F fix).
        logger.error(f"S3 upload failed for key {s3_key}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Internal error uploading file. Please try again.",
        ) from exc

    return s3_uri, s3_key


def _parse_form_additional_data(raw: str | None) -> dict | None:
    """Parse the additional_data form field (JSON string) into a dict."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"additional_data: invalid JSON — {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=422,
            detail="additional_data must be a JSON object.",
        )
    return parsed


_ANALYSIS_COLUMNS = """
    ca.audio_source_type,
    ca.call_id,
    ca.org_id,
    ca.insights_config_id,
    ca.created_at,
    ca.processed_at,
    ca.audio_duration_seconds,
    ca.non_talk_time_seconds,
    ca.audio_url,
    ca.api_source,
    ca.status,
    ca.sentiment_analysis,
    ca.agent_sentiment,
    ca.customer_sentiment,
    ca.transcript_turns,
    ca.key_topics,
    ca.recommendations,
    ca.overall_call_score,
    ca.overall_summary,
    ca.call_outcome,
    ca.talk_time_ratio,
    ca.custom_fields,
    ca.additional_data
"""


def _format_analysis_response(row: dict) -> dict:
    """Reshape a call_analysis row into the API response format."""
    return {
        "source_type": row.get("audio_source_type"),
        "call_id": row.get("call_id"),
        "org_id": row.get("org_id"),
        "insights_config_id": row.get("insights_config_id"),
        "call_start_time": None
        if row.get("api_source") == _API_SOURCE_EXTERNAL
        else row.get("created_at"),
        "call_end_time": None
        if row.get("api_source") == _API_SOURCE_EXTERNAL
        else row.get("processed_at"),
        "total_duration_seconds": row.get("audio_duration_seconds"),
        "non_talk_time_seconds": row.get("non_talk_time_seconds"),
        "recording_url": row.get("audio_url"),
        "insights_status": row.get("status"),
        "additional_data": row.get("additional_data"),
        "insights": {
            "overall_call_sentiment": row.get("sentiment_analysis"),
            "agent_sentiment": row.get("agent_sentiment"),
            "customer_sentiment": row.get("customer_sentiment"),
            "transcript": row.get("transcript_turns"),
            "key_topics": row.get("key_topics"),
            "actionable_insights": row.get("recommendations"),
            "overall_call_score": row.get("overall_call_score"),
            "summary": row.get("overall_summary"),
            "call_outcome": row.get("call_outcome"),
            "talk_time_ratio": row.get("talk_time_ratio"),
            "custom_insights": row.get("custom_fields"),
        },
    }


def _get_analysis_by_parent_call_request_id(
    request_id: str, org_id: str
) -> Optional[dict]:
    """Look up call_analysis via insights_jobs.parent_call_request_id."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT {_ANALYSIS_COLUMNS}
            FROM call_analysis ca
            JOIN insights_jobs j ON j.call_analysis_id = ca.id
            WHERE j.parent_call_request_id = %s AND j.org_id = %s
            """,
            (request_id, org_id),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def _get_analysis_by_job_id(job_id: str, org_id: str) -> Optional[dict]:
    """Look up call_analysis via insights_jobs.id."""
    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT {_ANALYSIS_COLUMNS}
            FROM call_analysis ca
            JOIN insights_jobs j ON j.call_analysis_id = ca.id
            WHERE j.id = %s AND j.org_id = %s
            """,
            (job_id, org_id),
        )
        row = cur.fetchone()
    return dict(row) if row else None


# ── request / response models ─────────────────────────────────────────────────


class ExternalJobRequest(BaseModel):
    audio_url: str
    insights_config_id: uuid.UUID | None = None  # uses org default if omitted
    priority: int = Field(default=100, ge=1, le=100)
    callback_url: str | None = None
    file_name: str | None = None
    additional_data: dict | None = None

    @field_validator("additional_data")
    @classmethod
    def validate_additional_data_size(cls, v: dict | None) -> dict | None:
        if v is not None and len(str(v)) > _ADDITIONAL_DATA_MAX_BYTES:
            raise ValueError(
                f"additional_data exceeds maximum allowed size of {_ADDITIONAL_DATA_MAX_BYTES} bytes"
            )
        return v


class ExternalJobResponse(BaseModel):
    request_id: uuid.UUID
    status: str = "queued"


# ── shared job-creation logic ─────────────────────────────────────────────────


def _submit_job(
    org_id: str,
    body: ExternalJobRequest,
    job_id: str | None = None,
) -> ExternalJobResponse:
    """Insert call_analysis + insights_jobs rows and return the job ID.

    Sync — safe to call directly from sync handlers or via asyncio.to_thread()
    from async handlers.

    Args:
        org_id:  Authenticated org UUID string.
        body:    Validated request model (audio_url already resolved for uploads).
        job_id:  Pre-generated UUID string for the insights_jobs row (upload path).
                 When None the DB generates the UUID via DEFAULT gen_random_uuid().
    """
    # Enforce 25 MB size limit for base64-encoded audio
    MAX_BASE64_BYTES = 25 * 1024 * 1024  # 25 MB
    if body.audio_url.startswith("data:audio"):
        if len(body.audio_url) > MAX_BASE64_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Base64 audio payload exceeds the {MAX_BASE64_BYTES // (1024 * 1024)} MB limit.",
            )

    config_id = body.insights_config_id
    if config_id is None:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id FROM insights_config
                WHERE org_id = %s AND is_default = TRUE
                LIMIT 1
                """,
                (org_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=422,
                detail="No default insights_config found for this org. "
                "Please set a default config or provide insights_config_id in the request body.",
            )
        config_id = row["id"]

    file_name = body.file_name or _file_name_from_url(body.audio_url)
    source_type = _detect_audio_source_type(body.audio_url)

    with handle_db_errors("create external job"):
        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO call_analysis (
                    file_name, insights_config_id, org_id,
                    audio_source_type, audio_url, status, api_source, callback_url,
                    additional_data
                )
                VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s)
                RETURNING id
                """,
                (
                    file_name,
                    str(config_id),
                    org_id,
                    source_type,
                    body.audio_url,
                    _API_SOURCE_EXTERNAL,
                    body.callback_url,
                    psycopg2_json(body.additional_data)
                    if body.additional_data is not None
                    else None,
                ),
            )
            analysis_id = cur.fetchone()["id"]

            # External uploads have no call_request — parent_call_request_id stays NULL.
            # The job's own id becomes the request_id for external jobs.
            if job_id is not None:
                # Upload path: use pre-generated ID so S3 key ↔ DB row share the UUID.
                cur.execute(
                    """
                    INSERT INTO insights_jobs (id, call_analysis_id, priority, org_id)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (job_id, str(analysis_id), body.priority, org_id),
                )
            else:
                # JSON / base64 path: let the DB generate the UUID.
                cur.execute(
                    """
                    INSERT INTO insights_jobs (call_analysis_id, priority, org_id)
                    VALUES (%s, %s, %s)
                    RETURNING id
                    """,
                    (str(analysis_id), body.priority, org_id),
                )
            returned_job_id = cur.fetchone()["id"]

    return ExternalJobResponse(request_id=returned_job_id)


# ── endpoints ─────────────────────────────────────────────────────────────────


@router.post("", response_model=ExternalJobResponse, status_code=202)
def submit_external_request(
    body: ExternalJobRequest,
    org_ctx: dict = Depends(verify_customer_api_key),
) -> ExternalJobResponse:
    """
    Queue an external audio URL for full insight pipeline processing.

    - **audio_url** — supports public HTTPS, s3://, gs://, r2://, az://, supabase://,
      or a base64 data URI (data:audio/...; max 25 MB)
    - **insights_config_id** — optional; falls back to the org's default config
    - **priority** — 1 (highest) to 100 (lowest), default 100
    - **callback_url** — overrides the config-level callback URL for this job only
    - **file_name** — optional display name; auto-extracted from URL if omitted

    Returns `request_id`. Poll `GET /insights/analyse?request_id=...` for status.
    For external uploads (no associated call), `request_id` is the insight job ID.

    To upload a file directly, use `POST /insights/analyse/upload` instead.
    """
    return _submit_job(org_id=org_ctx["org_id"], body=body)


@router.post("/upload", response_model=ExternalJobResponse, status_code=202)
async def submit_external_request_upload(
    file: UploadFile,
    insights_config_id: Optional[str] = Form(default=None),
    priority: int = Form(default=100, ge=1, le=100),
    callback_url: Optional[str] = Form(default=None),
    additional_data: Optional[str] = Form(default=None),
    org_ctx: dict = Depends(verify_customer_api_key),
) -> ExternalJobResponse:
    """
    Upload a call recording file directly for insight analysis.

    Supported formats: **mp3, mp4, wav, ogg, flac, m4a**

    Default max size: **25 MB** (hard ceiling: 500 MB).
    Override with `RECORDING_UPLOAD_MAX_FILE_SIZE_MB` env var.

    The file is validated (MIME type + magic bytes), streamed to S3, and queued
    for processing exactly like a caller-provided `s3://` URL — the worker
    pipeline is unchanged.

    S3 key: `{RECORDING_UPLOAD_S3_PREFIX}/{org_id}/{job_id}.{ext}`

    Returns `request_id`. Poll `GET /insights/analyse?request_id=...` for status.
    """
    org_id = org_ctx["org_id"]

    # Generate job_id upfront so the S3 key and insights_jobs row share the same UUID,
    # making uploaded objects directly traceable to their DB row.
    job_id = str(uuid.uuid4())

    # Validate all form fields before any I/O so a bad request never triggers
    # an S3 upload that would then need orphan cleanup.
    cfg_id: uuid.UUID | None = None
    if insights_config_id:
        try:
            cfg_id = uuid.UUID(insights_config_id)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="insights_config_id: invalid UUID format.",
            )
    else:
        # Check for default config before S3 upload to return 422 early.
        with get_cursor() as cur:
            cur.execute(
                "SELECT id FROM insights_config WHERE org_id = %s AND is_default = TRUE LIMIT 1",
                (org_id,),
            )
            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=422,
                    detail="No default insights_config found for this org. "
                    "Please set a default config or provide insights_config_id in the request body.",
                )
    additional_data_parsed = _parse_form_additional_data(additional_data)

    # boto3 upload is synchronous — run in a thread pool so the event
    # loop is not blocked during the upload (Bug A fix).
    s3_uri, s3_key = await asyncio.to_thread(
        _validate_and_upload_recording, file, org_id, job_id
    )

    safe_name = _sanitize_filename(file.filename)
    body = ExternalJobRequest(
        audio_url=s3_uri,
        file_name=safe_name,
        insights_config_id=cfg_id,
        priority=priority,
        callback_url=callback_url,
        additional_data=additional_data_parsed,
    )

    # _submit_job uses psycopg2 (sync) — also offload to thread pool so the
    # async handler never blocks the event loop (Bug A fix, continued).
    # If the DB insert fails after a successful S3 upload, attempt a best-effort
    # delete of the now-orphaned S3 object before propagating the error (Bug B fix).
    try:
        result = await asyncio.to_thread(_submit_job, org_id, body, job_id)
    except Exception:
        try:
            _get_s3_client().delete_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
            logger.warning(f"Cleaned up orphaned S3 object after DB failure: {s3_key}")
        except Exception as cleanup_exc:
            logger.error(
                f"Failed to clean up orphaned S3 object {s3_key}: {cleanup_exc}"
            )
        raise

    return result


@router.get("", summary="Get analysis result")
def get_analysis(
    request_id: uuid.UUID = Query(
        ...,
        description="Unified lookup: tries parent_call_request_id first, "
        "then falls back to insights_jobs.id",
    ),
    org_ctx: dict = Depends(verify_customer_api_key),
) -> Any:
    """
    Fetch the full `call_analysis` row for a completed or in-progress job.

    - **request_id** — for call-originated jobs this is the `call_request.id`;
      for external uploads this is the `insights_jobs.id` returned at submission time.

    Lookup priority:
    1. Match against `insights_jobs.parent_call_request_id`
    2. If no match, fall back to `insights_jobs.id`
    """
    org_id = org_ctx["org_id"]
    rid = str(request_id)

    # Try parent_call_request_id first, then fall back to insights_jobs.id
    row = _get_analysis_by_parent_call_request_id(rid, org_id)
    if not row:
        row = _get_analysis_by_job_id(rid, org_id)

    if not row:
        raise HTTPException(status_code=404, detail="Analysis not found")

    return _format_analysis_response(row)
