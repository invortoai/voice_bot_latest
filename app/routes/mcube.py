from dataclasses import dataclass
from typing import Optional, Union

from fastapi import APIRouter, HTTPException
from loguru import logger

from app.core.context import set_log_context, set_span_attrs
from app.models.schemas import (
    McubeConnectWebhookRequest,
    McubeConnectWebhookResponse,
    McubeStatusOkResponse,
)
from app.services import assistant_service, call_service, phone_number_service
from app.services.worker_pool import worker_pool

router = APIRouter(prefix="/mcube", tags=["MCube Webhooks"])


# =============================================================================
# Domain Models (Dataclasses)
# =============================================================================


@dataclass(frozen=True)
class CallIdentifiers:
    """Call identification information extracted from MCube webhook."""

    call_sid: str
    direction: str  # "inbound" or "outbound"
    is_outbound: bool
    caller: str
    called: str
    lookup_number: str


@dataclass(frozen=True)
class AssistantConfiguration:
    """Assistant configuration fetched from database."""

    phone_config: Optional[dict]
    assistant_config: Optional[dict]
    assistant_id: Optional[str]


# =============================================================================
# Helper Functions - Call Identifier Extraction
# =============================================================================


def _determine_call_identifiers(
    payload: McubeConnectWebhookRequest,
) -> CallIdentifiers:
    """Extract call identifiers from webhook payload.

    Maps callDirection, fromNumber, toNumber, callId to CallIdentifiers.
    """
    call_sid = payload.call_id
    direction = (payload.call_direction or "inbound").lower()
    is_outbound = direction == "outbound"

    if is_outbound:
        caller = payload.from_number or ""  # Agent number
        called = payload.to_number or ""  # Customer number
        lookup_number = caller[-10:]
    else:
        caller = payload.from_number or ""  # Customer number
        called = payload.to_number or ""  # DID number
        lookup_number = called[-10:]

    return CallIdentifiers(
        call_sid=call_sid,
        direction=direction,
        is_outbound=is_outbound,
        caller=caller,
        called=called,
        lookup_number=lookup_number,
    )


# =============================================================================
# Helper Functions - Assistant Configuration
# =============================================================================


async def _fetch_assistant_configuration(
    call_sid: str,
    is_outbound: bool,
    lookup_number: str,
) -> AssistantConfiguration:
    """Fetch assistant configuration with fallback logic.

    Priority:
    1. For outbound: Fetch from existing call record
    2. Fallback: Lookup by phone number

    Args:
        call_sid: Unique call identifier
        is_outbound: Whether call is outbound
        lookup_number: Phone number for fallback lookup

    Returns:
        AssistantConfiguration object with phone config, assistant config, and assistant ID
    """
    phone_config = None
    assistant_config = None
    assistant_id = None

    # Strategy 1: Fetch from existing call record
    if is_outbound:
        try:
            existing_call = call_service.get_by_sid(call_sid)
            if existing_call:
                assistant_id = existing_call.get("assistant_id")
                phone_number_id = existing_call.get("phone_number_id")

                if phone_number_id:
                    phone_config = phone_number_service.get_by_id(phone_number_id)

                if assistant_id:
                    assistant_config = assistant_service.get_by_id(assistant_id)

                logger.info(
                    f"Retrieved assistant from call record: assistant_id={assistant_id}"
                )
        except Exception as e:
            logger.error(f"Failed to fetch existing call record: {e}")

    # Strategy 2: Fallback to phone number lookup
    if not assistant_config:
        logger.info(f"Attempting fallback phone lookup: {lookup_number}")

        try:
            phone_config = phone_number_service.get_by_number(lookup_number)
            if phone_config and phone_config.get("assistant_id"):
                assistant_id = phone_config.get("assistant_id")
                assistant_config = assistant_service.get_by_id(assistant_id)
                logger.info(
                    f"Found assistant via phone lookup: assistant_id={assistant_id}"
                )
        except Exception as e:
            logger.error(f"Failed phone number lookup: {e}")

    return AssistantConfiguration(
        phone_config=phone_config,
        assistant_config=assistant_config,
        assistant_id=assistant_id,
    )


async def _create_or_update_call_record(
    call_sid: str,
    is_outbound: bool,
    caller: str,
    called: str,
    phone_config: Optional[dict],
    assistant_id: Optional[str],
    worker,
) -> None:
    """Create or update call record for connect webhook."""
    try:
        if is_outbound:
            call_service.update_worker_assignment(
                call_sid=call_sid,
                worker_instance_id=worker.instance_id,
                worker_host=worker.host,
                status="in-progress",
            )
            logger.info(f"Updated outbound call record: call_sid={call_sid}")
        else:
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
                provider="mcube",
            )
            logger.info(f"Created inbound call record: call_sid={call_sid}")
    except Exception as e:
        logger.error(f"Failed to log call to database: {e}", exc_info=True)


async def _update_call_status_and_release(
    call_sid: str,
    dial_status: str,
    end_time: Optional[str],
    answered_time: Optional[str],
    recording_url: Optional[str] = None,
) -> None:
    """Update call status and release worker if terminal.

    Shared logic for status/hangup from either connect or status webhook payloads.
    """
    logger.info(f"MCube status/hangup for call {call_sid}: dial_status={dial_status}")
    mapped_status = _map_dial_status(dial_status, end_time)
    try:
        duration_seconds = int(answered_time) if answered_time else 0
    except (ValueError, TypeError):
        duration_seconds = 0

    updated_call = None
    try:
        updated_call = call_service.update_status(
            call_sid=call_sid,
            status=mapped_status,
            duration_seconds=duration_seconds,
            recording_url=recording_url,
        )
        logger.info(f"Updated call status: call_sid={call_sid}, status={mapped_status}")
    except Exception as e:
        logger.error(
            f"Failed to update call status: call_sid={call_sid}, error={e}",
            exc_info=True,
        )

    if _is_terminal_status(mapped_status):
        logger.info(f"Releasing worker for call {call_sid} (status: {mapped_status})")
        await worker_pool.release_worker(call_sid)

        # Sync terminal outcome back to Supabase call_requests (best-effort, non-blocking)
        if updated_call:
            call_request_id = str(updated_call.get("parent_call_sid") or "")
            if call_request_id:
                await call_service.sync_call_request_outcome(
                    call_id=call_request_id,
                    call_status=mapped_status,
                    duration_seconds=duration_seconds,
                    call_start_time=updated_call.get("answered_at"),
                    call_end_time=updated_call.get("ended_at"),
                    recording_url=updated_call.get("recording_url"),
                )
    elif updated_call:
        # Sync mid-call status so call_requests stays in sync (e.g. in-progress)
        call_request_id = str(updated_call.get("parent_call_sid") or "")
        if call_request_id:
            await call_service.sync_call_request_outcome(
                call_id=call_request_id,
                call_status=mapped_status,
                call_start_time=updated_call.get("answered_at"),
            )


async def _handle_connect_hangup(
    payload: McubeConnectWebhookRequest,
) -> McubeStatusOkResponse:
    """Handle BUSY/ANSWER/EXECUTIVE BUSY/CANCEL/NOANSWER hangup events on /connect - update status and release worker."""
    await _update_call_status_and_release(
        call_sid=payload.call_id,
        dial_status=payload.dial_status or "",
        end_time=payload.endTime,
        answered_time=payload.answeredTime,
        recording_url=payload.filename,
    )
    return McubeStatusOkResponse()


# =============================================================================
# Helper Functions - Utility Functions
# =============================================================================


def _map_dial_status(dial_status: str, endtime: Optional[str]) -> str:
    """Map MCube dial_status to internal status.

    Args:
        dial_status: MCube dial_status value
        endtime: End time if call completed

    Returns:
        Mapped internal status
    """
    # If endtime is present, call is completed
    if endtime:
        return "completed"

    status_mapping = {
        "ANSWER": "in-progress",
        "CANCEL": "canceled",
        "Executive Busy": "busy",
        "Busy": "busy",
        "NoAnswer": "no-answer",
    }

    return status_mapping.get(dial_status, dial_status.lower())


def _is_terminal_status(status: str) -> bool:
    """Check if status is terminal (call ended).

    Args:
        status: Internal call status

    Returns:
        True if status is terminal, False otherwise
    """
    terminal_statuses = {"completed", "canceled", "busy", "no-answer", "failed"}
    return status in terminal_statuses


# =============================================================================
# Connect Endpoint - WebSocket URL (refurl)
# =============================================================================


def _is_connect_hangup(dial_status: Optional[str]) -> bool:
    """True if dial_status indicates a hangup event (BUSY or ANSWER) on /connect."""
    if not dial_status:
        return False
    normalized = dial_status.strip().upper()
    return normalized in ("BUSY", "ANSWER", "EXECUTIVE BUSY", "CANCEL", "NOANSWER")


@router.post(
    "/call",
    summary="MCube call - WebSocket URL or status/hangup",
    description="Single refurl: CONNECTING returns wss_url; BUSY/ANSWER update status and release worker.",
    response_model=Union[McubeConnectWebhookResponse, McubeStatusOkResponse],
)
async def mcube_call(payload: McubeConnectWebhookRequest):
    """Handle MCube call webhook - action by dial_status.

    - CONNECTING (or missing): assign worker, create/update call, return wss_url.
    - BUSY / ANSWER: hangup - update call status, release worker, return status ok.
    Configure in MCube: {PUBLIC_URL}/mcube/call (only this URL as refurl).
    """
    call_identifiers = _determine_call_identifiers(payload)
    set_log_context(call_sid=call_identifiers.call_sid, provider="mcube")
    set_span_attrs(
        **{"call_sid": call_identifiers.call_sid, "telephony.provider": "mcube"}
    )
    logger.info(
        f"MCube webhook received: {payload.call_direction} call {payload.call_id} status={payload.dial_status}"
    )
    if _is_connect_hangup(payload.dial_status):
        return await _handle_connect_hangup(payload)

    logger.debug(
        f"Fetching assistant configuration for {call_identifiers.direction} call {call_identifiers.call_sid}"
    )
    config = await _fetch_assistant_configuration(
        call_sid=call_identifiers.call_sid,
        is_outbound=call_identifiers.is_outbound,
        lookup_number=call_identifiers.lookup_number,
    )
    if config.phone_config:
        _org = str(config.phone_config.get("org_id", ""))
        set_log_context(org_id=_org)
        set_span_attrs(org_id=_org)
    logger.info(
        f"Assistant configuration loaded for call {call_identifiers.call_sid}: assistant_id={config.assistant_id or 'none'}"
    )

    try:
        if not config.assistant_config:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "CONFIGURATION_NOT_FOUND",
                        "message": "No assistant configuration found for this call",
                    }
                },
            )

        if call_identifiers.is_outbound:
            logger.debug(
                f"Assigning worker for outbound call {call_identifiers.call_sid}"
            )
            worker = await worker_pool.get_worker_for_call(call_identifiers.call_sid)
            if not worker:
                logger.warning(
                    f"No pre-assigned worker for outbound call {call_identifiers.call_sid}, assigning new worker"
                )
                worker = await worker_pool.get_and_assign_worker(
                    call_identifiers.call_sid
                )
        else:
            logger.debug(
                f"Assigning worker for inbound call {call_identifiers.call_sid}"
            )
            worker = await worker_pool.get_and_assign_worker(call_identifiers.call_sid)

        if not worker:
            logger.warning(
                f"No available workers for {call_identifiers.direction} call {call_identifiers.call_sid}"
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "code": "NO_WORKERS_AVAILABLE",
                        "message": "All workers are currently busy. Please try again later.",
                    }
                },
            )

        logger.info(
            f"Worker {worker.instance_id} assigned to {call_identifiers.direction} call {call_identifiers.call_sid}"
        )

        await _create_or_update_call_record(
            call_sid=call_identifiers.call_sid,
            is_outbound=call_identifiers.is_outbound,
            caller=call_identifiers.caller,
            called=call_identifiers.called,
            phone_config=config.phone_config,
            assistant_id=config.assistant_id,
            worker=worker,
        )

        if not call_identifiers.is_outbound:
            worker_pool.trigger_prewarm_nowait(worker, call_identifiers.call_sid)

        ws_url = worker.get_ws_url(f"/ws/mcube/{call_identifiers.call_sid}")

        logger.info(f"Call routed to worker {worker.instance_id}: {ws_url}")

        return McubeConnectWebhookResponse(wss_url=ws_url)

    except HTTPException:
        raise
    except Exception:
        await worker_pool.release_worker(call_identifiers.call_sid)
        logger.warning(
            f"Releasing worker after connect failure for call {call_identifiers.call_sid}"
        )
        raise
