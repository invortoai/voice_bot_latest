import asyncio
import base64
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, List

import boto3
from botocore.config import Config
from fastapi import APIRouter, HTTPException, Request, status
from loguru import logger

from app.core.context import set_log_context, set_span_attrs
from app.core.rate_limiter import limiter
from app.core.serialization import json_safe
from app.config import (
    JAMBONZ_WEBHOOK_SECRET,
    S3_ACCESS_KEY_ID,
    S3_BUCKET_NAME,
    S3_RECORDING_FETCH_RETRY_DELAY,
    S3_REGION,
    S3_SECRET_ACCESS_KEY,
)
from app.models.schemas import (
    JambonzStatusWebhookRequest,
    JambonzWebhookRequest,
)
from app.services import assistant_service, call_service, phone_number_service
from app.services.worker_pool import worker_pool

router = APIRouter(prefix="/jambonz", tags=["Jambonz Webhooks"])


def _verify_jambonz_webhook(request: Request) -> None:
    """Validate Jambonz webhook authentication via HTTP Basic Auth.

    Jambonz sends credentials as Authorization: Basic base64(username:password).
    We compare only the password against JAMBONZ_WEBHOOK_SECRET.
    Raises 503 if JAMBONZ_WEBHOOK_SECRET is not configured.
    Raises 403 if credentials are missing, malformed, or incorrect.
    """
    if not JAMBONZ_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook authentication not configured",
        )

    auth_header = request.headers.get("Authorization", "")
    client_ip = request.client.host if request.client else "unknown"

    if not auth_header.startswith("Basic "):
        logger.warning(
            f"Jambonz webhook auth FAILED: missing or non-Basic Authorization header "
            f"from {client_ip} [{request.method} {request.url.path}]. "
            "Call will be rejected (403). "
            "Fix: configure HTTP Basic Auth in your Jambonz application settings "
            "with the value of JAMBONZ_WEBHOOK_SECRET as the password."
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing Jambonz webhook authorization",
        )

    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        password = decoded.split(":", 1)[1] if ":" in decoded else decoded
    except Exception as exc:
        logger.warning(
            f"Jambonz webhook auth FAILED: could not decode Basic Auth header "
            f"from {client_ip} [{request.method} {request.url.path}]: {exc}. "
            "Call will be rejected (403)."
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Jambonz webhook authorization",
        )

    if not hmac.compare_digest(password, JAMBONZ_WEBHOOK_SECRET):
        logger.warning(
            f"Jambonz webhook auth FAILED: incorrect password "
            f"from {client_ip} [{request.method} {request.url.path}]. "
            "Call will be rejected (403). "
            "Fix: ensure the password in your Jambonz application Basic Auth settings "
            "matches JAMBONZ_WEBHOOK_SECRET."
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Jambonz webhook secret",
        )

    logger.debug(
        f"Jambonz webhook auth passed from {client_ip} [{request.method} {request.url.path}]"
    )


# Tracks call_sids with a recording fetch task already in flight.
# Prevents duplicate fetches when Jambonz fires multiple terminal status events.
_recording_fetch_in_flight: set[str] = set()


async def _fetch_and_store_recording_url(call_sid: str) -> None:
    """Verify recording exists in S3 and store the S3 URI.

    Attempts immediately, then retries with S3_RECORDING_FETCH_RETRY_DELAY backoff.
    3 total attempts (immediate + 2 retries @ 30s each = ~60s window).
    Stores the S3 URI (s3://bucket/key) so presigned URLs can be generated at read time.
    """
    if not S3_ACCESS_KEY_ID or not S3_SECRET_ACCESS_KEY or not S3_BUCKET_NAME:
        logger.debug(
            f"S3 not configured, skipping recording fetch for call_sid={call_sid}"
        )
        return

    # Use the call's created_at from DB for the S3 key path (Jambonz names
    # the file based on when the call started, not when we fetch it).
    call_record = call_service.get_by_sid(call_sid)
    if call_record and call_record.get("created_at"):
        call_dt = call_record["created_at"]
    else:
        call_dt = datetime.now(timezone.utc)
        logger.warning(
            f"call record not found for call_sid={call_sid}, using current time for S3 key"
        )
    key = f"{call_dt.year}/{call_dt.month:02d}/{call_dt.day:02d}/{call_sid}.mp3"
    s3_uri = f"s3://{S3_BUCKET_NAME}/{key}"

    def _s3_verify() -> None:
        """Verify the object exists in S3 (raises if not found)."""
        s3 = boto3.client(
            "s3",
            aws_access_key_id=S3_ACCESS_KEY_ID,
            aws_secret_access_key=S3_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4"),
            region_name=S3_REGION,
        )
        s3.head_object(Bucket=S3_BUCKET_NAME, Key=key)

    max_attempts = 3
    loop = asyncio.get_running_loop()
    for attempt in range(1, max_attempts + 1):
        try:
            await loop.run_in_executor(None, _s3_verify)
            updated = call_service.set_recording_url(call_sid, s3_uri)
            logger.info(
                f"Recording URL stored for call {call_sid} (S3, attempt {attempt}): {s3_uri}"
            )
            # Sync recording_url to call_requests via parent_call_sid (= call_requests.id).
            # For inbound calls (no call_requests row), parent_call_sid is None → no-op.
            if updated:
                call_request_id = str(updated.get("parent_call_sid") or "")
                if call_request_id:
                    await call_service.sync_call_request_outcome(
                        call_id=call_request_id,
                        call_status="completed",
                        recording_url=s3_uri,
                    )
            return
        except Exception as e:
            if attempt < max_attempts:
                logger.warning(
                    f"S3 recording not ready for call_sid={call_sid} "
                    f"(attempt {attempt}/{max_attempts}), "
                    f"retrying in {S3_RECORDING_FETCH_RETRY_DELAY}s: {e}"
                )
                await asyncio.sleep(S3_RECORDING_FETCH_RETRY_DELAY)
            else:
                logger.warning(
                    f"Could not fetch S3 recording for call_sid={call_sid} "
                    f"key={key} after {max_attempts} attempts: {e}"
                )


@router.post(
    "/call",
    summary="Handle inbound and outbound calls",
    description="Webhook for Jambonz calls (both inbound and answered outbound). Configure this URL in your Jambonz application.",
    response_model=List[dict],
    responses={
        200: {
            "description": "Jambonz call-control verbs",
            "content": {
                "application/json": {
                    "example": [
                        {"verb": "answer"},
                        {
                            "verb": "listen",
                            "url": "wss://example.com/ws/jambonz",
                            "mixType": "mono",
                            "sampleRate": 8000,
                            "passDtmf": True,
                            "bidirectionalAudio": {
                                "enabled": True,
                                "streaming": True,
                                "sampleRate": 8000,
                            },
                            "metadata": {
                                "call_sid": "abc123",
                                "call_type": "inbound",
                                "assistant_id": "uuid",
                            },
                        },
                    ]
                }
            },
        }
    },
)
@limiter.limit("60/minute")
async def jambonz_call(request: Request, payload: JambonzWebhookRequest):
    """Handle Jambonz call webhook.

    Jambonz invokes this endpoint when a call is routed to this application.
    This handles BOTH inbound calls AND outbound calls when they are answered.

    For outbound calls, the 'customerData' or 'tag' field contains all the
    metadata we need (passed when initiating the call).

    Configure in Jambonz Portal: `{PUBLIC_URL}/jambonz/call`
    """
    _verify_jambonz_webhook(request)

    webhook_received_at = time.time()  # wall clock for cross-process transport_hop calc

    # Use snake_case fields from the validated payload
    call_sid = payload.call_sid or ""
    caller = payload.from_number or ""
    called = payload.to_number or ""

    # Resolve the call key early so it is available for atomic worker assignment.
    # Use Jambonz's call_sid when present, otherwise generate a stable UUID.
    call_key = call_sid if call_sid else str(uuid.uuid4())
    set_log_context(call_sid=call_key, provider="jambonz")
    set_span_attrs(**{"call_sid": call_key, "telephony.provider": "jambonz"})

    # Determine call direction from the direction field
    is_outbound = payload.direction == "outbound"

    # Parse metadata from 'customer_data' or 'tag' for outbound calls
    # Jambonz passes our tag data in 'customerData' field in webhooks
    tag_data = payload.customer_data or payload.tag or {}
    if isinstance(tag_data, str):
        try:
            tag_data = json.loads(tag_data)
        except Exception:
            tag_data = {}

    direction = "outbound" if is_outbound else "inbound"
    logger.info(
        f"{direction.capitalize()} call received: {call_key} from {caller} to {called}"
    )

    # For outbound calls, use metadata from tag; for inbound, fetch from DB
    phone_config = None
    assistant_id = None
    worker = None

    if is_outbound:
        # For outbound calls, retrieve pre-assigned worker
        assistant_id = tag_data.get("assistant_id")
        worker = await worker_pool.get_worker_for_call(call_sid)
        if not worker:
            logger.warning(
                f"No pre-assigned worker found for outbound call {call_sid}, getting a new one"
            )
            worker = await worker_pool.get_and_assign_worker(call_sid)
    else:
        # For inbound calls, fetch config and get a new worker
        try:
            phone_config = phone_number_service.get_by_number(called)
            if phone_config:
                _org = str(phone_config.get("org_id", ""))
                set_log_context(org_id=_org)
                set_span_attrs(org_id=_org)
            if phone_config and phone_config.get("assistant_id"):
                assistant_id = phone_config.get("assistant_id")
            logger.info(f"Phone config loaded: assistant_id={assistant_id or 'none'}")
        except Exception as e:
            logger.error(f"Failed to fetch phone/assistant configuration: {e}")

        worker = await worker_pool.get_and_assign_worker(call_key)

    if not worker:
        logger.warning(f"Worker assignment failed: no capacity for call {call_key}")
        # Mirror the Twilio behavior: answer, speak a message (Deepgram TTS via
        # Jambonz defaults), then hang up.
        return [
            {"verb": "answer"},
            {
                "verb": "say",
                "text": "Sorry, all agents are busy. Please try again later.",
                # Most Jambonz apps already have defaults set, but this makes the
                # intent explicit and keeps behavior stable across tenants.
                "synthesizer": {"vendor": "deepgram", "language": "en-US"},
            },
            {"verb": "hangup"},
        ]
    logger.info(f"Worker assigned: {worker.instance_id}")

    try:
        if is_outbound:
            # For outbound calls, update status to in-progress
            call_service.update_status(call_sid=call_key, status="in-progress")
        else:
            # For inbound calls, create a new record
            call_service.create(
                call_sid=call_key,
                org_id=phone_config.get("org_id") if phone_config else None,
                direction="inbound",
                from_number=caller,
                to_number=called,
                phone_number_id=phone_config.get("id") if phone_config else None,
                assistant_id=assistant_id,
                status="initiated",
                worker_instance_id=worker.instance_id,
                worker_host=worker.host,
                provider="jambonz",
            )
            inbound_prewarm_payload = None
            if phone_config:
                try:
                    _ac = (
                        assistant_service.get_by_id(assistant_id)
                        if assistant_id
                        else None
                    )
                except Exception:
                    _ac = None
                if _ac:
                    inbound_prewarm_payload = {
                        "assistant_config": json_safe(_ac),
                        "phone_config": json_safe(phone_config),
                        "custom_params": {},
                        "provider_name": "jambonz",
                    }
            worker_pool.trigger_prewarm_nowait(
                worker, call_key, inbound_prewarm_payload
            )
    except Exception as e:
        logger.error(f"Failed to log Jambonz call to database: {e}")

    try:
        ws_url = worker.get_ws_url("/ws/jambonz")
        logger.info(f"Call routed to worker {worker.instance_id} at {ws_url}")
        logger.info(f"customerData: {tag_data}")

        # Jambonz call-control format is a list of verbs.
        # We answer the call, then 'listen' to stream audio bidirectionally to our websocket.
        # Jambonz `listen` supports `metadata` (not `customParameters`).
        # This metadata is echoed back by Jambonz in the initial websocket JSON frame.
        #
        # We only pass essential call info and assistant_id here.
        # The worker will fetch the full assistant configuration from the database.
        metadata: dict[str, Any] = {
            "call_sid": call_key,
            "caller": caller,
            "called": called,
            "call_type": "outbound" if is_outbound else "inbound",
        }

        if assistant_id:
            metadata["assistant_id"] = assistant_id

        # Default to 8k unless Jambonz explicitly provides a sample_rate.
        # 8k is the most universally compatible telephony rate; we can safely move
        # to 16k once we confirm Jambonz is actually negotiating 16k at runtime.
        # Audio sent FROM Jambonz -> our websocket.
        in_sample_rate = payload.sample_rate or 8000
        # Audio we send back TO Jambonz (streaming bidirectional).
        out_sample_rate = in_sample_rate

        metadata["jambonz_audio_in_sample_rate"] = in_sample_rate
        metadata["jambonz_audio_out_sample_rate"] = out_sample_rate
        metadata["jambonz_bidirectional_streaming"] = True

        # Latency instrumentation: runner self-times + wall clock for transport hop
        runner_webhook_ms = round((time.time() - webhook_received_at) * 1000, 1)
        metadata["runner_webhook_ms"] = runner_webhook_ms
        metadata["webhook_completed_at"] = time.time()

        logger.info(
            f"Sending metadata to Jambonz listen verb: call_type={metadata.get('call_type')}, called={metadata.get('called')}, caller={metadata.get('caller')}"
        )
        logger.debug(f"Full metadata: {metadata}")

        return [
            {"verb": "answer"},
            {
                "verb": "listen",
                "url": ws_url,
                "mixType": "mono",
                "sampleRate": in_sample_rate,
                "passDtmf": True,
                # Per Jambonz docs: enable streaming bidirectional audio and send raw
                # linear16 PCM as *binary websocket frames*.
                "bidirectionalAudio": {
                    "enabled": True,
                    "streaming": True,
                    "sampleRate": out_sample_rate,
                },
                "metadata": metadata,
            },
        ]
    except Exception as e:
        await worker_pool.release_worker(call_key)
        logger.error(f"Failed to build Jambonz response for call {call_key}: {e}")
        raise


@router.post(
    "/status",
    summary="Call status callback",
    description="Webhook for Jambonz call status updates. Configure as status_callback in your application.",
)
@limiter.limit("120/minute")
async def jambonz_call_status(request: Request, payload: JambonzStatusWebhookRequest):
    """Handle Jambonz call status updates.

    Keeps worker_pool state in sync and updates call status in DB.
    Configure in Jambonz Portal: `{PUBLIC_URL}/jambonz/status`
    """
    _verify_jambonz_webhook(request)

    call_sid = payload.call_sid or ""
    call_status = payload.call_status or ""

    if not call_sid:
        logger.warning(
            f"Jambonz status webhook received with no call_sid "
            f"(call_status={call_status!r}) — cannot release worker. "
            "Check your Jambonz application status callback configuration."
        )
        return {"status": "ok"}

    logger.info(f"Call status update: {call_sid} -> {call_status}")

    # Map Jambonz status values to our schema.
    # We preserve distinct terminal statuses (busy, no-answer) for proper reporting.
    normalized = (call_status or "").lower()
    if normalized in ("completed", "ended", "hangup", "hangup_complete"):
        mapped = "completed"
    elif normalized in ("failed", "error"):
        mapped = "failed"
    elif normalized in ("canceled", "cancelled"):
        mapped = "cancelled"
    elif normalized == "busy":
        mapped = "busy"
    elif normalized == "no-answer":
        mapped = "no-answer"
    elif normalized in ("in-progress", "inprogress", "answered"):
        mapped = "in-progress"
    elif normalized in ("trying", "ringing", "early-media"):
        mapped = normalized
    elif normalized:
        mapped = normalized
    else:
        mapped = "in-progress"

    updated_call = None
    if call_sid:
        try:
            updated_call = call_service.update_status(
                call_sid=call_sid, status=mapped, duration_seconds=payload.duration
            )
            logger.info(
                f"Jambonz call status update: {call_sid} -> {mapped}, duration={payload.duration}"
            )
        except Exception as e:
            logger.error(f"Failed to update Jambonz call status in database: {e}")

    # Release worker on terminal statuses.
    terminal_statuses = {
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
    }
    if normalized in terminal_statuses and call_sid:
        logger.info(f"Releasing worker for terminal call status: {call_status}")
        await worker_pool.release_worker(call_sid)

        if mapped == "completed" and call_sid not in _recording_fetch_in_flight:
            _recording_fetch_in_flight.add(call_sid)

            async def _fetch_and_cleanup(sid: str = call_sid) -> None:
                try:
                    await _fetch_and_store_recording_url(sid)
                finally:
                    _recording_fetch_in_flight.discard(sid)

            asyncio.create_task(_fetch_and_cleanup())
        elif mapped == "completed":
            logger.debug(
                f"Recording fetch already in flight for call_sid={call_sid}, skipping duplicate"
            )

        # Sync terminal outcome back to Supabase call_requests (best-effort, non-blocking)
        if updated_call:
            call_request_id = str(updated_call.get("parent_call_sid") or "")
            if call_request_id:
                await call_service.sync_call_request_outcome(
                    call_id=call_request_id,
                    call_status=mapped,
                    duration_seconds=payload.duration,
                    call_start_time=updated_call.get("answered_at"),
                    call_end_time=updated_call.get("ended_at"),
                    recording_url=updated_call.get("recording_url"),
                )
    elif updated_call and call_sid:
        # Sync mid-call status so call_requests stays in sync (e.g. in-progress, ringing)
        call_request_id = str(updated_call.get("parent_call_sid") or "")
        if call_request_id:
            await call_service.sync_call_request_outcome(
                call_id=call_request_id,
                call_status=mapped,
                call_start_time=updated_call.get("answered_at"),
            )

    return {"status": "ok"}
