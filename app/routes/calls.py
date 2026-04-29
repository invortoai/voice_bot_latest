import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.core.serialization import json_safe
from app.core.auth import verify_customer_api_key, verify_global_key_with_org
from app.core.database import get_cursor
from app.models.schemas import (
    CallInitiateRequest,
    CallInitiateResponse,
    OutboundCallRequest,
    OutboundCallResponse,
)
from app.services import assistant_service, call_service, phone_number_service
from app.services import call_request as call_request_service
from app.services.outbound.registry import get_provider
from app.services.worker_pool import worker_pool

_INTERNAL_CALL_FIELDS = ("worker_host", "worker_instance_id", "provider_metadata")


def _mask_phone(number: str) -> str:
    if not number or len(number) < 4:
        return "***"
    return "***" + number[-4:]


def _sanitize_call_list(call: dict) -> dict:
    """Mask PII in call list view."""
    c = dict(call)
    if c.get("from_number"):
        c["from_number"] = _mask_phone(c["from_number"])
    if c.get("to_number"):
        c["to_number"] = _mask_phone(c["to_number"])
    for field in _INTERNAL_CALL_FIELDS:
        c.pop(field, None)
    return c


def _sanitize_call_detail(call: dict) -> dict:
    """Strip infrastructure fields from call detail view."""
    c = dict(call)
    for field in _INTERNAL_CALL_FIELDS:
        c.pop(field, None)
    return c


def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


router = APIRouter(tags=["Calls"])


@router.post(
    "/calls",
    response_model=CallInitiateResponse,
    status_code=202,
    summary="Initiate an outbound call",
    description=(
        "Customer-facing call initiation endpoint. "
        "Accepts lead/opportunity context, enqueues the call, and returns a request_id immediately. "
        "The org-queue-processor dispatches the actual call. "
        "Authenticated via X-API-Key (org API key)."
    ),
)
async def initiate_call(
    request: CallInitiateRequest,
    org_ctx: dict = Depends(verify_customer_api_key),
):
    """
    Trigger an outbound call with contextual lead data.

    - **assistant_id** / **phone_number_id** — required; must belong to your org
    - **to_number** — E.164 format, e.g. `+917022xxxxxx`
    - **input_variables** — injected as `{{variable_name}}` tokens in the conversation script
    - **invorto_campaign_id** — optional; call follows campaign dialling rules when provided
    - **call_time** — ISO 8601; omit to trigger immediately
    - **priority** — 1 (highest) to 100 (lowest), default 5
    - **callback_url** — HTTPS webhook URL to receive the post-call stats payload
    - **additional_data** — stored and returned in post-call payload; not injected into the script

    Returns `request_id` — use `GET /call-stats/{request_id}` to poll for status and outcome.
    """
    org_id = org_ctx["org_id"]
    campaign_id = (
        str(request.invorto_campaign_id) if request.invorto_campaign_id else None
    )

    try:
        # ── 1. Validate org (active + has minutes) ────────────────────────────
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT is_active, minutes_consumed, total_minutes_ordered
                FROM organizations WHERE id = %s
                """,
                (org_id,),
            )
            org = cur.fetchone()
        if not org:
            raise HTTPException(status_code=404, detail="Organisation not found")
        if not org["is_active"]:
            raise HTTPException(status_code=403, detail="Organisation is not active")
        if org["minutes_consumed"] >= org["total_minutes_ordered"]:
            raise HTTPException(
                status_code=402, detail="Organisation has no minutes remaining"
            )

        # ── 2. Validate assistant ─────────────────────────────────────────────
        assistant = assistant_service.get_by_id(
            str(request.assistant_id), org_id=org_id
        )
        if not assistant:
            raise HTTPException(status_code=404, detail="Assistant not found")
        if not assistant.get("is_active", True):
            raise HTTPException(status_code=400, detail="Assistant is not active")

        # ── 3. Validate phone number ──────────────────────────────────────────
        phone = phone_number_service.get_by_id(
            str(request.phone_number_id), org_id=org_id
        )
        if not phone:
            raise HTTPException(status_code=404, detail="Phone number not found")
        if not phone.get("is_active", True):
            raise HTTPException(status_code=400, detail="Phone number is inactive")
        if not phone.get("is_outbound_enabled", True):
            raise HTTPException(
                status_code=400,
                detail="Outbound calls are disabled for this phone number",
            )

        # ── 4. Validate campaign if provided ──────────────────────────────────
        if campaign_id:
            with get_cursor() as cur:
                cur.execute(
                    "SELECT id FROM campaigns WHERE id = %s AND org_id = %s",
                    (campaign_id, org_id),
                )
                if not cur.fetchone():
                    raise HTTPException(
                        status_code=404,
                        detail="Campaign not found or does not belong to this org",
                    )

        # ── 5. Validate call_time if provided ─────────────────────────────────
        if request.call_time:
            try:
                datetime.fromisoformat(request.call_time.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="call_time must be a valid ISO 8601 datetime (e.g. 2026-03-20T10:00:00Z)",
                )

        # ── 6. Duplicate check ────────────────────────────────────────────────
        if call_request_service.check_duplicate(
            to_number=request.to_number,
            org_id=org_id,
            campaign_id=campaign_id,
        ):
            raise HTTPException(
                status_code=409,
                detail=f"{request.to_number} already has a pending call request",
            )

        # ── 7. Enqueue ────────────────────────────────────────────────────────
        row = call_request_service.create(
            org_id=org_id,
            assistant_id=str(request.assistant_id),
            phone_number_id=str(request.phone_number_id),
            to_number=request.to_number,
            input_variables=request.input_variables,
            external_customer_id=request.external_customer_id,
            campaign_id=campaign_id,
            callback_url=request.callback_url,
            scheduled_at=request.call_time,
            priority=request.priority or 5,
            additional_data=request.additional_data,
        )

        return CallInitiateResponse(
            request_id=str(row["id"]),
            status=row["status"],
            message="Call request accepted. Use request_id to poll for status and outcome.",
        )

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Error creating call request")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/calls",
    summary="List calls",
    description="Retrieve a paginated list of calls with optional filters.",
)
async def list_calls(
    phone_number_id: Optional[str] = None,
    assistant_id: Optional[str] = None,
    status: Optional[str] = None,
    direction: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    org_ctx: dict = Depends(verify_customer_api_key),
):
    """List all calls with optional filtering.

    - **phone_number_id**: Filter by phone number
    - **assistant_id**: Filter by assistant
    - **status**: Filter by call status (initiated, in-progress, completed, failed)
    - **direction**: Filter by direction (inbound, outbound)
    """
    try:
        calls = call_service.get_many(
            org_id=org_ctx["org_id"],
            phone_number_id=phone_number_id,
            assistant_id=assistant_id,
            status=status,
            direction=direction,
            limit=limit,
            offset=offset,
        )
        return {
            "calls": [_sanitize_call_list(c) for c in calls],
            "total": len(calls),
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        logger.exception("Error listing calls")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/calls/{call_id}",
    summary="Get call details",
    description="Retrieve details for a specific call by ID or SID.",
)
async def get_call(
    call_id: str,
    org_ctx: dict = Depends(verify_customer_api_key),
):
    """Get a single call by its ID or call SID."""
    try:
        call = (
            call_service.get_by_id(call_id, org_id=org_ctx["org_id"])
            if _is_valid_uuid(call_id)
            else None
        )
        if not call:
            call = call_service.get_by_sid(call_id, org_id=org_ctx["org_id"])
        if not call:
            raise HTTPException(status_code=404, detail="Call not found")
        return _sanitize_call_detail(call)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error getting call")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/call/outbound",
    summary="Initiate outbound call",
    description=(
        "Start an outbound call. The telephony provider is determined automatically "
        "from the phone number configuration (twilio, jambonz, or mcube). "
        "Supply call_id to enforce end-to-end UUID consistency with the caller's "
        "call_requests record."
    ),
    response_model=OutboundCallResponse,
)
async def initiate_outbound_call(
    request: OutboundCallRequest,
    org_ctx: dict = Depends(verify_global_key_with_org),
):
    """Initiate an outbound call via any configured provider.

    Provider is resolved from `phone_number.provider` — no need to specify it in the URL.

    When `call_id` is supplied (from call_requests.id), that UUID is stored as
    calls.id so the same identifier flows through every system. When omitted a
    fresh UUID is generated.
    """
    # Use caller-supplied call_id (end-to-end UUID consistency) or generate new
    if request.call_id:
        call_id = str(request.call_id)  # supplied UUID becomes calls.id (E2E tracing)
        parent_call_id = call_id  # also stored as parent_call_sid for outcome sync
    else:
        call_id = str(uuid.uuid4())
        parent_call_id = None
    org_id = org_ctx["org_id"]

    # ── Fetch + validate phone config ─────────────────────────────────────────
    try:
        phone_config = phone_number_service.get_by_id(
            str(request.phone_number_id), org_id=org_id
        )
    except Exception as e:
        logger.exception("Database error fetching phone config")
        raise HTTPException(status_code=500, detail="Database error")

    if not phone_config:
        raise HTTPException(status_code=404, detail="Phone number not found")

    if not phone_config.get("is_outbound_enabled"):
        raise HTTPException(
            status_code=400, detail="Outbound calls disabled for this number"
        )

    if not phone_config.get("is_active", True):
        raise HTTPException(status_code=400, detail="Phone number is inactive")

    # ── Fetch + validate assistant config ─────────────────────────────────────
    try:
        assistant_config = assistant_service.get_by_id(
            str(request.assistant_id), org_id=org_id
        )
    except Exception as e:
        logger.exception("Failed to fetch assistant configuration")
        raise HTTPException(
            status_code=500, detail="Failed to fetch assistant configuration"
        )

    if not assistant_config:
        raise HTTPException(status_code=404, detail="Assistant not found")

    if not assistant_config.get("is_active", True):
        raise HTTPException(status_code=400, detail="Assistant is not active")

    # ── Capacity checks (fail fast before reserving a worker) ─────────────────
    # Phone number concurrent limit
    max_concurrent = phone_config.get("max_concurrent_calls")
    if max_concurrent:
        active = call_service.count_active_calls(
            phone_number_id=str(request.phone_number_id), org_id=org_id
        )
        if active >= max_concurrent:
            logger.warning(
                f"Concurrent call limit reached for phone {request.phone_number_id}: "
                f"{active}/{max_concurrent} active"
            )
            raise HTTPException(
                status_code=429,
                detail=f"Phone number at concurrent call limit ({max_concurrent})",
            )

    # Phone number daily limit
    max_daily = phone_config.get("max_calls_per_day")
    if max_daily:
        today = call_service.count_calls_today(
            phone_number_id=str(request.phone_number_id), org_id=org_id
        )
        if today >= max_daily:
            logger.warning(
                f"Daily call limit reached for phone {request.phone_number_id}: "
                f"{today}/{max_daily} today"
            )
            raise HTTPException(
                status_code=429,
                detail=f"Phone number daily call limit reached ({max_daily}/day)",
            )

    # ── Resolve provider and validate credentials (fail fast) ─────────────────
    provider_name = phone_config.get("provider", "twilio")
    provider = get_provider(provider_name)
    provider.validate_credentials(phone_config)

    # ── Reserve worker ────────────────────────────────────────────────────────
    logger.info(
        f"Outbound call request: provider={provider_name} reservation_id={call_id}"
    )
    worker = await worker_pool.get_and_assign_worker(call_id)
    if not worker:
        logger.warning(
            f"Worker assignment failed: no capacity for outbound call {call_id}"
        )
        raise HTTPException(status_code=503, detail="No available workers")
    logger.info(f"Worker assigned: {worker.instance_id}")

    try:
        # ── Prewarm worker before dialing (phone only rings after worker is ready) ──
        prewarm_payload = {
            "assistant_config": json_safe(assistant_config),
            "phone_config": json_safe(phone_config),
            "custom_params": json_safe(request.custom_params or {}),
            "provider_name": provider_name,
        }
        ready = await worker_pool.send_prewarm_and_wait(
            worker, call_id, prewarm_payload
        )
        if not ready:
            logger.warning(
                f"[prewarm] call_id={call_id}: worker not ready before dial, proceeding anyway"
            )

        result = await provider.initiate(
            call_id=call_id,
            phone_config=phone_config,
            assistant_config=assistant_config,
            worker=worker,
            to_number=request.to_number,
            custom_params=request.custom_params,
        )

        await worker_pool.reassign_call_sid(call_id, result.call_sid)
        logger.info(f"Call SID updated: {call_id} → {result.call_sid}")

        # Re-key prewarm cache from call_id → real call_sid (fire-and-forget)
        worker_pool.trigger_prewarm_reassign_nowait(worker, call_id, result.call_sid)

        try:
            call_service.create(
                call_sid=result.call_sid,
                org_id=org_id,
                direction="outbound",
                from_number=result.from_number,
                to_number=request.to_number,
                phone_number_id=str(request.phone_number_id),
                assistant_id=str(request.assistant_id),
                status="initiated",
                worker_instance_id=worker.instance_id,
                worker_host=worker.host,
                custom_params=request.custom_params or {},
                provider=provider_name,
                call_id=call_id,
                parent_call_sid=parent_call_id,
            )
        except Exception as e:
            logger.exception("Failed to log call to database")

        logger.info(f"Call routed to worker {worker.instance_id}: {result.call_sid}")
        return OutboundCallResponse(
            call_sid=result.call_sid,
            call_id=parent_call_id or call_id,
            to_number=request.to_number,
            from_number=result.from_number,
            phone_number_id=str(request.phone_number_id),
            worker_id=worker.instance_id,
            status="initiated",
            provider=provider_name,
        )

    except Exception:
        await worker_pool.release_worker(call_id)
        logger.warning(f"Worker released after outbound call failure: {call_id}")
        raise


# ---------------------------------------------------------------------------
# Legacy aliases — kept for backward compatibility.
# Both delegate directly to the unified endpoint above.
# ---------------------------------------------------------------------------


@router.post(
    "/call/outbound/jambonz",
    summary="Initiate Jambonz outbound call (deprecated)",
    description="Deprecated. Use POST /call/outbound instead — provider is resolved automatically.",
    response_model=OutboundCallResponse,
)
async def initiate_jambonz_outbound_call(
    request: OutboundCallRequest,
    org_ctx: dict = Depends(verify_global_key_with_org),
):
    return await initiate_outbound_call(request, org_ctx)
