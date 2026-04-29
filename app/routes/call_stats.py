from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.core.auth import verify_customer_api_key
from app.models.schemas import CallStatRecord, WebhookDeliveryRecord
from app.services import call_service

router = APIRouter(prefix="/call-stats", tags=["Call Stats"])


@router.get(
    "",
    response_model=list[CallStatRecord],
    summary="List call stats",
    description=(
        "Returns per-call stat records for terminal calls (answered, missed, busy, failed, rejected). "
        "Each record includes the full original initiation payload. "
        "Authenticated via X-API-Key header."
    ),
)
async def list_call_stats(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    call_status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    org_ctx: dict = Depends(verify_customer_api_key),
):
    """
    Poll call stats for a date range.

    - **from_date**: ISO 8601 datetime, e.g. `2024-01-01T00:00:00Z`
    - **to_date**: ISO 8601 datetime, e.g. `2024-01-31T23:59:59Z`
    - **call_status**: Filter by outcome — `answered`, `missed`, `busy`, `failed`, `rejected`
    - **limit** / **offset**: Pagination
    """
    try:
        return call_service.get_call_stats(
            org_id=org_ctx["org_id"],
            from_date=from_date,
            to_date=to_date,
            call_status=call_status,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        logger.error(f"Error fetching call stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{request_id}",
    response_model=CallStatRecord,
    summary="Get stats for a single call",
    description=(
        "Returns the full stats record for one call by its `request_id`. "
        "Includes the original initiation payload."
    ),
)
async def get_single_call_stat(
    request_id: UUID,
    org_ctx: dict = Depends(verify_customer_api_key),
):
    """Retrieve stats for a specific call using its `request_id`."""
    try:
        record = call_service.get_call_stat(str(request_id), org_id=org_ctx["org_id"])
        if not record:
            raise HTTPException(status_code=404, detail="Call not found")
        return record
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching call stat {request_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{request_id}/webhook-deliveries",
    response_model=list[WebhookDeliveryRecord],
    summary="List webhook delivery attempts for a call",
    description=(
        "Returns all webhook delivery attempts for a specific call-stat record, "
        "ordered chronologically. Includes HTTP response details and retry state "
        "for each attempt. Authenticated via X-API-Key header."
    ),
)
async def get_call_webhook_deliveries(
    request_id: UUID,
    org_ctx: dict = Depends(verify_customer_api_key),
):
    """Retrieve the webhook delivery log for a specific call by its `request_id`."""
    try:
        return call_service.get_webhook_deliveries(
            request_id=str(request_id),
            org_id=org_ctx["org_id"],
        )
    except Exception as e:
        logger.error(f"Error fetching webhook deliveries for call {request_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
