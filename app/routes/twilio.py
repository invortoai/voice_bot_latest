from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from loguru import logger
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

from app.core.context import set_log_context, set_span_attrs
from app.core.rate_limiter import limiter
from app.services import call_service, phone_number_service
from app.services.worker_pool import worker_pool
from app.utils.twilio_signature import validate_twilio_signature

router = APIRouter(prefix="/twilio", tags=["Twilio Webhooks"])


async def _verify_twilio_webhook(request: Request, auth_token: str | None) -> None:
    """Validate Twilio webhook signature. Rejects if auth_token is missing or signature invalid."""
    if not auth_token:
        raise HTTPException(status_code=403, detail="Cannot verify Twilio signature")
    await validate_twilio_signature(request, auth_token)


@router.post(
    "/incoming",
    summary="Handle inbound call",
    description="Webhook for Twilio inbound calls. Configure this URL in your Twilio Console.",
)
@limiter.limit("60/minute")
async def twilio_incoming_call(request: Request):
    """Handle incoming Twilio call.

    Twilio invokes this endpoint when a call is received on your phone number.
    Returns TwiML instructions to connect the call audio to a worker websocket.

    Configure in Twilio Console: `{PUBLIC_URL}/twilio/incoming`
    """
    form_data = await request.form()
    call_sid = str(form_data.get("CallSid", ""))
    caller = str(form_data.get("From", ""))
    called = str(form_data.get("To", ""))
    set_log_context(call_sid=call_sid, provider="twilio")
    set_span_attrs(**{"call_sid": call_sid, "telephony.provider": "twilio"})

    logger.info(f"Inbound call received: {call_sid} from {caller} to {called}")

    # Fetch phone number configuration to get associated assistant
    phone_config = None
    assistant_id = None

    try:
        phone_config = phone_number_service.get_by_number(called)
        if phone_config:
            _org = str(phone_config.get("org_id", ""))
            set_log_context(org_id=_org)
            set_span_attrs(org_id=_org)
        if phone_config and phone_config.get("assistant_id"):
            assistant_id = str(phone_config.get("assistant_id"))
        logger.info(f"Phone config loaded: assistant_id={assistant_id or 'none'}")
    except Exception as e:
        logger.error(f"Failed to fetch phone/assistant configuration: {e}")

    # Validate Twilio webhook signature
    auth_token = (phone_config or {}).get("provider_credentials", {}).get("auth_token")
    await _verify_twilio_webhook(request, auth_token)

    worker = await worker_pool.get_and_assign_worker(call_sid)

    if not worker:
        logger.warning(f"Worker assignment failed: no capacity for call {call_sid}")
        response = VoiceResponse()
        response.say("Sorry, all agents are busy. Please try again later.")
        response.hangup()
        return Response(content=str(response), media_type="application/xml")
    logger.info(f"Worker assigned: {worker.instance_id}")

    try:
        call_service.create(
            call_sid=call_sid,
            org_id=phone_config.get("org_id") if phone_config else None,
            direction="inbound",
            from_number=caller,
            to_number=called,
            phone_number_id=str(phone_config.get("id")) if phone_config else None,
            assistant_id=assistant_id,
            status="initiated",
            worker_instance_id=worker.instance_id,
            worker_host=worker.host,
            provider_metadata=dict(form_data),
            provider="twilio",
        )
        worker_pool.trigger_prewarm_nowait(worker, call_sid)
    except Exception as e:
        logger.error(f"Failed to log call to database: {e}")

    ws_url = worker.get_ws_url()
    logger.info(f"Call routed to worker {worker.instance_id} at {ws_url}")

    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=ws_url)

    stream.parameter(name="call_sid", value=call_sid)
    stream.parameter(name="caller", value=caller)
    stream.parameter(name="called", value=called)
    stream.parameter(name="call_type", value="inbound")

    connect.append(stream)
    response.append(connect)
    response.pause(
        length=phone_config.get("max_call_duration_seconds", 3600)
        if phone_config
        else 3600
    )

    return Response(content=str(response), media_type="application/xml")


@router.post(
    "/status",
    summary="Call status callback",
    description="Webhook for Twilio call status updates. Configure as StatusCallback in your calls.",
)
@limiter.limit("120/minute")
async def twilio_call_status(request: Request):
    """Handle Twilio call status updates.

    Keeps worker_pool state in sync and updates call status in DB.
    Configure in Twilio Console or pass as StatusCallback when creating calls.
    """
    form_data = await request.form()
    call_sid = str(form_data.get("CallSid", ""))
    call_status = str(form_data.get("CallStatus", ""))
    call_duration = form_data.get("CallDuration")
    set_log_context(call_sid=call_sid, provider="twilio")
    call_duration = str(call_duration) if call_duration is not None else None

    if not call_sid:
        logger.warning(
            f"Twilio status webhook received with no CallSid "
            f"(CallStatus={call_status!r}) — cannot release worker. "
            "Check your Twilio StatusCallback configuration."
        )
        return Response(content="", status_code=200)

    logger.info(f"Call status update: {call_sid} -> {call_status}")

    # Validate Twilio webhook signature via the call's phone number credentials
    call_record = call_service.get_by_sid(call_sid)
    sig_token = None
    if call_record and call_record.get("phone_number_id"):
        phone_cfg = phone_number_service.get_by_id(str(call_record["phone_number_id"]))
        sig_token = (phone_cfg or {}).get("provider_credentials", {}).get("auth_token")
    await _verify_twilio_webhook(request, sig_token)

    duration = int(call_duration) if call_duration else None
    updated_call = None

    try:
        updated_call = call_service.update_status(
            call_sid=call_sid,
            status=call_status,
            duration_seconds=duration,
        )
    except Exception as e:
        logger.error(f"Failed to update call status in database: {e}")

    if call_status in ["completed", "failed", "busy", "no-answer", "canceled"]:
        logger.info(f"Releasing worker for terminal call status: {call_status}")
        await worker_pool.release_worker(call_sid)

        # Sync terminal outcome back to Supabase call_requests (best-effort, non-blocking)
        if updated_call:
            call_request_id = str(updated_call.get("parent_call_sid") or "")
            if call_request_id:
                await call_service.sync_call_request_outcome(
                    call_id=call_request_id,
                    call_status=call_status,
                    duration_seconds=duration,
                    call_start_time=updated_call.get("answered_at"),
                    call_end_time=updated_call.get("ended_at"),
                    recording_url=updated_call.get("recording_url"),
                )
    elif updated_call:
        # Sync mid-call status so call_requests stays in sync (e.g. in-progress, ringing)
        call_request_id = str(updated_call.get("parent_call_sid") or "")
        if call_request_id:
            await call_service.sync_call_request_outcome(
                call_id=call_request_id,
                call_status=call_status,
                call_start_time=updated_call.get("answered_at"),
            )

    return {"status": "ok"}


@router.post(
    "/recording-status",
    summary="Twilio recording status callback",
    description=(
        "Webhook called by Twilio when a call recording is ready. "
        "Stores the RecordingUrl in the calls table. "
        "Configure as recordingStatusCallback on the call with recordingStatusCallbackEvent=['completed']."
    ),
)
@limiter.limit("60/minute")
async def twilio_recording_status(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    recording_url = (form_data.get("RecordingUrl") or "").strip() or None
    recording_status = form_data.get("RecordingStatus", "")

    logger.info(
        f"Twilio recording status: call_sid={call_sid} status={recording_status} "
        f"has_url={bool(recording_url)}"
    )

    # Validate Twilio webhook signature via the call's phone number credentials.
    # Validate Twilio webhook signature via the call's phone number credentials
    sig_token = None
    if call_sid:
        rec_call = call_service.get_by_sid(str(call_sid))
        if rec_call and rec_call.get("phone_number_id"):
            phone_cfg = phone_number_service.get_by_id(str(rec_call["phone_number_id"]))
            sig_token = (
                (phone_cfg or {}).get("provider_credentials", {}).get("auth_token")
            )
    await _verify_twilio_webhook(request, sig_token)

    if call_sid and recording_url and recording_status == "completed":
        try:
            updated = call_service.set_recording_url(call_sid, recording_url)
            logger.info(f"Recording URL stored for call {call_sid}")
            # Sync recording_url to call_requests via parent_call_sid (= call_requests.id).
            # For inbound calls (no call_requests row), parent_call_sid is None → no-op.
            if updated:
                call_request_id = str(updated.get("parent_call_sid") or "")
                if call_request_id:
                    await call_service.sync_call_request_outcome(
                        call_id=call_request_id,
                        call_status="completed",
                        recording_url=recording_url,
                    )
        except Exception as e:
            logger.error(f"Failed to update recording_url for call {call_sid}: {e}")

    return {"status": "ok"}
