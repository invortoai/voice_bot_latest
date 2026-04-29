"""
Webhook delivery service for post-call-stat webhooks.

Replaces the PG trigger + Supabase edge function approach with a single
Python implementation inside the runner. Handles:

  - Callback URL resolution (call_request → campaign → assistant/insights_config)
  - Payload construction (built fresh at delivery time so recording_url is current)
  - HMAC-SHA256 signing (Stripe-style, consistent with insight webhooks)
  - SSRF validation
  - HTTP delivery with retry + exponential backoff

Delivery is triggered explicitly via trigger_pending_webhooks() once
recording_url is available. Retries are picked up by the background
WebhookWorker poller. The poller also handles fallback for completed calls
where no recording callback ever arrives (after 30s).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any, Optional

import httpx
from loguru import logger
from psycopg2.extras import Json

from app.config import (
    WEBHOOK_BACKOFF_SECONDS,
    WEBHOOK_CALLBACK_SECRET,
    WEBHOOK_DELIVERY_TIMEOUT_SECONDS,
    WEBHOOK_MAX_ATTEMPTS,
    WEBHOOK_PENDING_FALLBACK_SECONDS,
)
from app.core.database import get_cursor
from app.services.s3_service import presign_recording_url
from app.utils.ssrf_validator import SSRFError, validate_callback_url

# ── Status display mapping (matches PG trigger exactly) ──────────────────────

_STATUS_DISPLAY = {
    "completed": "answered",
    "no-answer": "missed",
    "cancelled": "rejected",
    # busy and failed pass through unchanged
}

_EVENT_TYPE_MAP = {
    "completed": "call.completed",
    "failed": "call.failed",
    "no-answer": "call.no_answer",
    "busy": "call.busy",
    "cancelled": "call.cancelled",
}

_DEFAULT_CALLBACK_EVENTS = [
    "call.completed",
    "call.failed",
    "call.no_answer",
    "call.busy",
    "call.cancelled",
]


# ── Enqueue ──────────────────────────────────────────────────────────────────


def enqueue_webhook(
    call_request_id: str, org_id: str, call_status: str
) -> Optional[str]:
    """Resolve callback URL, insert a pending webhook_deliveries row (idempotent),
    and return the delivery ID.

    Does NOT fire delivery — call trigger_pending_webhooks() when data is ready.
    Returns None if no callback URL is configured or the event is not subscribed.
    """
    event_type = _EVENT_TYPE_MAP.get(call_status)
    if not event_type:
        return None

    # ── Resolve callback URL: call_request → campaign → assistant ────────
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT cr.callback_url, cr.callback_events, cr.campaign_id, cr.bot_id
                FROM call_requests cr
                WHERE cr.id = %s AND cr.org_id = %s
                """,
                (call_request_id, org_id),
            )
            cr_row = cur.fetchone()
    except Exception as exc:
        logger.error(
            f"webhook_enqueue: failed to fetch call_request {call_request_id}: {exc}"
        )
        return None

    if not cr_row:
        return None

    callback_url = cr_row.get("callback_url") or ""

    # Fallback to campaign
    if not callback_url and cr_row.get("campaign_id"):
        try:
            with get_cursor() as cur:
                cur.execute(
                    "SELECT callback_url FROM campaigns WHERE id = %s",
                    (cr_row["campaign_id"],),
                )
                camp_row = cur.fetchone()
                if camp_row:
                    callback_url = camp_row.get("callback_url") or ""
        except Exception as exc:
            logger.warning(f"webhook_enqueue: campaign lookup failed: {exc}")

    # Fallback to assistant → insights_config
    if not callback_url and cr_row.get("bot_id"):
        try:
            with get_cursor() as cur:
                cur.execute(
                    """
                    SELECT ic.callback_url
                    FROM   assistants      a
                    JOIN   insights_config ic ON ic.id = a.insights_config_id
                    WHERE  a.id = %s
                      AND  ic.callback_url IS NOT NULL
                      AND  ic.callback_url <> ''
                    """,
                    (cr_row["bot_id"],),
                )
                ic_row = cur.fetchone()
                if ic_row:
                    callback_url = ic_row.get("callback_url") or ""
        except Exception as exc:
            logger.warning(f"webhook_enqueue: insights_config lookup failed: {exc}")

    if not callback_url:
        return None  # no callback configured at any level

    # ── Check subscribed events ──────────────────────────────────────────
    callback_events = cr_row.get("callback_events") or _DEFAULT_CALLBACK_EVENTS
    if event_type not in callback_events:
        return None

    # ── Idempotency: return existing pending delivery if one already exists ──
    # Prevents duplicate rows when recording-status callbacks re-trigger
    # sync_call_request_outcome for the same terminal event.
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id FROM webhook_deliveries
                WHERE call_request_id = %s AND event_type = %s
                  AND status IN ('pending', 'failed')
                LIMIT 1
                """,
                (call_request_id, event_type),
            )
            existing = cur.fetchone()
            if existing:
                logger.debug(
                    f"webhook_enqueue: existing delivery {existing['id']} found for "
                    f"call_request_id={call_request_id} event={event_type}, skipping insert"
                )
                return str(existing["id"])
    except Exception as exc:
        logger.warning(f"webhook_enqueue: idempotency check failed: {exc}")

    try:
        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO webhook_deliveries (
                    org_id, call_request_id, event_type, webhook_url, payload,
                    next_retry_at
                ) VALUES (%s, %s, %s, %s, %s,
                    CASE WHEN %s = 'call.completed'
                         THEN NOW() + make_interval(secs => %s)
                         ELSE NULL
                    END)
                RETURNING id
                """,
                (
                    org_id,
                    call_request_id,
                    event_type,
                    callback_url,
                    Json({}),
                    event_type,
                    WEBHOOK_PENDING_FALLBACK_SECONDS,
                ),
            )
            row = cur.fetchone()
            delivery_id = str(row["id"]) if row else None
    except Exception as exc:
        logger.error(f"webhook_enqueue: INSERT failed for {call_request_id}: {exc}")
        return None

    logger.info(
        f"webhook_enqueue: enqueued delivery_id={delivery_id} "
        f"call_request_id={call_request_id} event={event_type} url={callback_url}"
    )

    return delivery_id


# ── Trigger pending deliveries ───────────────────────────────────────────────


def trigger_pending_webhooks(call_request_id: str) -> None:
    """Fire any pending webhook deliveries for this call_request immediately.

    Called once recording_url is available (or when no recording is expected)
    so the payload is built fresh with the URL already in the DB.
    Safe to call multiple times — idempotency check in enqueue_webhook ensures
    only one delivery row exists per event.
    """
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id FROM webhook_deliveries
                WHERE call_request_id = %s AND status = 'pending'
                """,
                (call_request_id,),
            )
            rows = cur.fetchall()
    except Exception as exc:
        logger.error(
            f"trigger_pending_webhooks: query failed for {call_request_id}: {exc}"
        )
        return

    if not rows:
        return

    try:
        loop = asyncio.get_running_loop()
        for row in rows:
            delivery_id = str(row["id"])
            loop.create_task(_fire_delivery(delivery_id))
    except RuntimeError:
        logger.debug(
            f"trigger_pending_webhooks: no event loop for {call_request_id}, "
            "poller will pick up"
        )


async def _fire_delivery(delivery_id: str) -> None:
    """Fire-and-forget wrapper for triggered deliveries."""
    try:
        await deliver_webhook(delivery_id)
    except Exception as exc:
        logger.error(f"webhook_trigger: delivery {delivery_id} failed: {exc}")


# ── Payload builder ──────────────────────────────────────────────────────────


def build_payload(call_request_id: str) -> Optional[dict[str, Any]]:
    """Build the PRD2-compliant webhook payload from fresh call_requests data.

    Builds a consistent `initiation_payload` from table columns so the shape
    is identical for API, campaign, and quick_call sources.
    Presigns recording_url if it's an S3 URI.
    """
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id, source, call_status, call_direction,
                       call_start_time, call_end_time,
                       call_duration_seconds, recording_url,
                       bot_id, phone_number_id, phone_number,
                       lead_id, campaign_id,
                       custom_params, additional_data,
                       callback_url, scheduled_at, priority
                FROM call_requests
                WHERE id = %s
                """,
                (call_request_id,),
            )
            row = cur.fetchone()
    except Exception as exc:
        logger.error(
            f"webhook_build_payload: query failed for {call_request_id}: {exc}"
        )
        return None

    if not row:
        return None

    raw_status = row.get("call_status") or ""
    display_status = _STATUS_DISPLAY.get(raw_status, raw_status)

    # Build E.164 to_number from stored phone_number
    phone = row.get("phone_number") or ""
    to_number = phone if phone.startswith("+") else f"+91{phone}" if phone else None

    # Consistent initiation_payload — same shape for all sources
    initiation_payload = {
        "assistant_id": str(row["bot_id"]) if row.get("bot_id") else None,
        "phone_number_id": str(row["phone_number_id"])
        if row.get("phone_number_id")
        else None,
        "to_number": to_number,
        "input_variables": row.get("custom_params") or {},
        "external_customer_id": row.get("lead_id"),
        "invorto_campaign_id": str(row["campaign_id"])
        if row.get("campaign_id")
        else None,
        "callback_url": row.get("callback_url"),
        "call_time": row["scheduled_at"].isoformat()
        if row.get("scheduled_at")
        else None,
        "priority": row.get("priority"),
        "additional_data": row.get("additional_data") or {},
    }

    return {
        "request_id": str(row["id"]),
        "call_status": display_status,
        "call_direction": row.get("call_direction") or "outbound",
        "call_start_time": row["call_start_time"].isoformat()
        if row.get("call_start_time")
        else None,
        "call_end_time": row["call_end_time"].isoformat()
        if row.get("call_end_time")
        else None,
        "total_duration_seconds": row.get("call_duration_seconds"),
        "recording_url": presign_recording_url(row.get("recording_url"), expiry=3600),
        "initiation_payload": initiation_payload,
    }


# ── HMAC signing ─────────────────────────────────────────────────────────────


def _sign_body(body: str, secret: str) -> tuple[str, str]:
    """HMAC-SHA256 signing following Stripe webhook pattern."""
    timestamp = str(int(time.time()))
    message = f"{timestamp}.{body}"
    signature = hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return timestamp, f"sha256={signature}"


def _resolve_callback_secret(call_request_id: str) -> str:
    """Resolve HMAC secret: insights_config.callback_secret → global fallback."""
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT ic.callback_secret
                FROM call_requests cr
                JOIN assistants      a  ON a.id  = cr.bot_id
                JOIN insights_config ic ON ic.id = a.insights_config_id
                WHERE cr.id = %s
                  AND ic.callback_secret IS NOT NULL
                  AND ic.callback_secret <> ''
                """,
                (call_request_id,),
            )
            row = cur.fetchone()
            if row:
                return row["callback_secret"]
    except Exception:
        pass
    return WEBHOOK_CALLBACK_SECRET


# ── Delivery ─────────────────────────────────────────────────────────────────


def _is_retryable_status(status_code: Optional[int]) -> bool:
    if status_code is None:
        return True  # network error
    return status_code >= 500 or status_code == 429


async def deliver_webhook(delivery_id: str) -> bool:
    """Deliver a single webhook. Returns True on success.

    Builds payload fresh from call_requests (so retries get updated data
    like recording_url), presigns S3 URLs, SSRF-validates the target,
    signs with HMAC, and POSTs.
    Updates webhook_deliveries row with the result.
    """
    # Fetch delivery row (payload is already stored from enqueue time)
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id, call_request_id, event_type, webhook_url,
                       attempt_number, max_attempts, status, payload
                FROM webhook_deliveries
                WHERE id = %s
                """,
                (delivery_id,),
            )
            delivery = cur.fetchone()
    except Exception as exc:
        logger.error(f"webhook_deliver: fetch failed for {delivery_id}: {exc}")
        return False

    if not delivery:
        return False

    # Already delivered or exhausted — skip (guards against duplicate delivery)
    if delivery["status"] not in ("pending", "failed"):
        return False

    call_request_id = str(delivery["call_request_id"])
    webhook_url = delivery["webhook_url"]
    event_type = delivery["event_type"]
    attempt_number = delivery["attempt_number"] or 1
    max_attempts = delivery["max_attempts"] or WEBHOOK_MAX_ATTEMPTS

    # Always rebuild payload fresh so retries pick up recording_url once available
    payload = build_payload(call_request_id)
    if not payload:
        logger.error(f"webhook_deliver: empty payload for {delivery_id}")
        _update_delivery(
            delivery_id, "failed", attempt_number, error_message="Empty payload"
        )
        return False

    # SSRF validation
    try:
        await validate_callback_url(webhook_url)
    except SSRFError as exc:
        logger.error(f"webhook_deliver: SSRF rejected {webhook_url}: {exc}")
        _update_delivery(
            delivery_id, "exhausted", attempt_number, error_message=f"SSRF: {exc}"
        )
        return False

    # Resolve signing secret
    secret = _resolve_callback_secret(call_request_id)

    # Build request body + headers
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Invorto-Event": event_type,
        "User-Agent": "InvortoRunner/1.0",
    }
    if secret:
        timestamp, signature = _sign_body(body, secret)
        headers["X-Invorto-Timestamp"] = timestamp
        headers["X-Invorto-Signature"] = signature

    # POST
    start_ms = time.monotonic()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                webhook_url,
                content=body.encode(),
                headers=headers,
                timeout=float(WEBHOOK_DELIVERY_TIMEOUT_SECONDS),
            )
        elapsed_ms = int((time.monotonic() - start_ms) * 1000)
        response_body = response.text[:2000] if response.text else None

        if 200 <= response.status_code < 300:
            _update_delivery(
                delivery_id,
                "delivered",
                attempt_number,
                status_code=response.status_code,
                response_body=response_body,
                response_time_ms=elapsed_ms,
            )
            logger.info(
                f"webhook_delivered: delivery_id={delivery_id} "
                f"url={webhook_url} status={response.status_code} ms={elapsed_ms}"
            )
            return True

        # Non-2xx
        error_msg = f"HTTP {response.status_code}"
        is_final = attempt_number >= max_attempts or not _is_retryable_status(
            response.status_code
        )
        _update_delivery_failure(
            delivery_id,
            attempt_number,
            max_attempts,
            status_code=response.status_code,
            response_body=response_body,
            response_time_ms=elapsed_ms,
            error_message=error_msg,
            is_final=is_final,
        )
        logger.warning(
            f"webhook_failed: delivery_id={delivery_id} attempt={attempt_number} "
            f"status={response.status_code} error={error_msg}"
        )
        return False

    except httpx.TimeoutException:
        elapsed_ms = int((time.monotonic() - start_ms) * 1000)
        _update_delivery_failure(
            delivery_id,
            attempt_number,
            max_attempts,
            response_time_ms=elapsed_ms,
            error_message=f"Timed out after {WEBHOOK_DELIVERY_TIMEOUT_SECONDS}s",
            is_final=attempt_number >= max_attempts,
        )
        return False
    except httpx.HTTPError as exc:
        elapsed_ms = int((time.monotonic() - start_ms) * 1000)
        _update_delivery_failure(
            delivery_id,
            attempt_number,
            max_attempts,
            response_time_ms=elapsed_ms,
            error_message=str(exc),
            is_final=attempt_number >= max_attempts,
        )
        return False


# ── DB helpers ───────────────────────────────────────────────────────────────


def _update_delivery(
    delivery_id: str,
    status: str,
    attempt_number: int,
    *,
    status_code: Optional[int] = None,
    response_body: Optional[str] = None,
    response_time_ms: Optional[int] = None,
    error_message: Optional[str] = None,
) -> None:
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                UPDATE webhook_deliveries
                SET status               = %s,
                    attempt_number       = %s,
                    last_attempted_at    = NOW(),
                    delivered_at         = CASE WHEN %s = 'delivered' THEN NOW() ELSE delivered_at END,
                    response_status_code = %s,
                    response_body        = %s,
                    response_time_ms     = %s,
                    error_message        = %s,
                    next_retry_at        = NULL
                WHERE id = %s
                """,
                (
                    status,
                    attempt_number,
                    status,
                    status_code,
                    response_body,
                    response_time_ms,
                    error_message,
                    delivery_id,
                ),
            )
    except Exception as exc:
        logger.error(f"webhook: failed to update delivery {delivery_id}: {exc}")


def _update_delivery_failure(
    delivery_id: str,
    attempt_number: int,
    max_attempts: int,
    *,
    status_code: Optional[int] = None,
    response_body: Optional[str] = None,
    response_time_ms: Optional[int] = None,
    error_message: Optional[str] = None,
    is_final: bool = False,
) -> None:
    """Update a failed delivery — either schedule retry or mark exhausted."""
    if is_final:
        _update_delivery(
            delivery_id,
            "exhausted",
            attempt_number,
            status_code=status_code,
            response_body=response_body,
            response_time_ms=response_time_ms,
            error_message=error_message,
        )
        return

    # Schedule retry
    next_attempt = attempt_number + 1
    backoff_idx = min(attempt_number - 1, len(WEBHOOK_BACKOFF_SECONDS) - 1)
    backoff = WEBHOOK_BACKOFF_SECONDS[backoff_idx] if WEBHOOK_BACKOFF_SECONDS else 30

    try:
        with get_cursor() as cur:
            cur.execute(
                """
                UPDATE webhook_deliveries
                SET status               = 'failed',
                    attempt_number       = %s,
                    last_attempted_at    = NOW(),
                    response_status_code = %s,
                    response_body        = %s,
                    response_time_ms     = %s,
                    error_message        = %s,
                    next_retry_at        = NOW() + make_interval(secs => %s)
                WHERE id = %s
                """,
                (
                    next_attempt,
                    status_code,
                    response_body,
                    response_time_ms,
                    error_message,
                    backoff,
                    delivery_id,
                ),
            )
    except Exception as exc:
        logger.error(f"webhook: failed to schedule retry for {delivery_id}: {exc}")
