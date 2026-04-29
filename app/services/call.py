from typing import Optional
from datetime import datetime, timezone

from psycopg2.extras import Json

from app.core.database import get_cursor
from app.services.s3_service import presign_recording_url


def create(
    call_sid: str,
    direction: str,
    from_number: str,
    to_number: str,
    org_id: Optional[str] = None,
    phone_number_id: Optional[str] = None,
    assistant_id: Optional[str] = None,
    status: str = "initiated",
    worker_instance_id: Optional[str] = None,
    worker_host: Optional[str] = None,
    custom_params: Optional[dict] = None,
    provider_metadata: Optional[dict] = None,
    provider: str = "twilio",
    call_id: Optional[str] = None,
    parent_call_sid: Optional[str] = None,
) -> dict:
    """Create a new call record."""
    with get_cursor() as cur:
        if call_id:
            cur.execute(
                """
                INSERT INTO calls (
                    id, call_sid, parent_call_sid, org_id, direction, from_number, to_number, phone_number_id,
                    assistant_id, status, started_at, worker_instance_id, worker_host,
                    custom_params, provider_metadata, provider
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """,
                (
                    call_id,
                    call_sid,
                    parent_call_sid,
                    org_id,
                    direction,
                    from_number,
                    to_number,
                    phone_number_id,
                    assistant_id,
                    status,
                    datetime.utcnow(),
                    worker_instance_id,
                    worker_host,
                    Json(custom_params or {}),
                    Json(provider_metadata or {}),
                    provider,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO calls (
                    call_sid, parent_call_sid, org_id, direction, from_number, to_number, phone_number_id,
                    assistant_id, status, started_at, worker_instance_id, worker_host,
                    custom_params, provider_metadata, provider
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """,
                (
                    call_sid,
                    parent_call_sid,
                    org_id,
                    direction,
                    from_number,
                    to_number,
                    phone_number_id,
                    assistant_id,
                    status,
                    datetime.utcnow(),
                    worker_instance_id,
                    worker_host,
                    Json(custom_params or {}),
                    Json(provider_metadata or {}),
                    provider,
                ),
            )
        return dict(cur.fetchone())


def get_by_sid(call_sid: str, org_id: Optional[str] = None) -> Optional[dict]:
    with get_cursor() as cur:
        if org_id:
            cur.execute(
                "SELECT * FROM calls WHERE call_sid = %s AND org_id = %s",
                (call_sid, org_id),
            )
        else:
            cur.execute("SELECT * FROM calls WHERE call_sid = %s", (call_sid,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_by_id(call_id: str, org_id: Optional[str] = None) -> Optional[dict]:
    with get_cursor() as cur:
        if org_id:
            cur.execute(
                "SELECT * FROM calls WHERE id = %s AND org_id = %s",
                (call_id, org_id),
            )
        else:
            cur.execute("SELECT * FROM calls WHERE id = %s", (call_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def update_status(
    call_sid: str,
    status: str,
    ended_at: Optional[datetime] = None,
    duration_seconds: Optional[int] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    recording_url: Optional[str] = None,
) -> Optional[dict]:
    with get_cursor() as cur:
        if status in (
            "completed",
            "busy",
            "no-answer",
            "canceled",
            "cancelled",
            "failed",
        ):
            cur.execute(
                """
                UPDATE calls
                SET status = %s, ended_at = %s, duration_seconds = %s,
                    error_code = %s, error_message = %s,
                    recording_url = COALESCE(%s, recording_url)
                WHERE call_sid = %s
                RETURNING *
            """,
                (
                    status,
                    ended_at or datetime.utcnow(),
                    duration_seconds,
                    error_code,
                    error_message,
                    recording_url,
                    call_sid,
                ),
            )
        elif status == "in-progress":
            cur.execute(
                """
                UPDATE calls
                SET status = %s, answered_at = %s
                WHERE call_sid = %s
                RETURNING *
            """,
                (status, datetime.utcnow(), call_sid),
            )
        else:
            cur.execute(
                "UPDATE calls SET status = %s WHERE call_sid = %s RETURNING *",
                (status, call_sid),
            )
        row = cur.fetchone()
        return dict(row) if row else None


def set_recording_url(call_sid: str, recording_url: str) -> Optional[dict]:
    """Update only the recording_url for a call once the provider makes it available."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE calls SET recording_url = %s WHERE call_sid = %s RETURNING *",
            (recording_url, call_sid),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def update_worker_assignment(
    call_sid: str,
    worker_instance_id: str,
    worker_host: str,
    status: str = "in-progress",
) -> Optional[dict]:
    """Update worker assignment for a call (used when outbound call is answered)."""
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE calls
            SET status = %s, answered_at = %s,
                worker_instance_id = %s, worker_host = %s
            WHERE call_sid = %s
            RETURNING *
        """,
            (status, datetime.utcnow(), worker_instance_id, worker_host, call_sid),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def update_provider_metadata(call_sid: str, provider_metadata: dict) -> Optional[dict]:
    """Update provider metadata for a call."""
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE calls
            SET provider_metadata = %s
            WHERE call_sid = %s
            RETURNING *
        """,
            (Json(provider_metadata), call_sid),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def add_transcript_message(call_sid: str, role: str, content: str) -> Optional[dict]:
    message = {
        "role": role,
        "content": content,
        "timestamp": datetime.utcnow().isoformat(),
    }
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE calls
            SET transcript = transcript || %s::jsonb
            WHERE call_sid = %s
            RETURNING *
        """,
            (Json([message]), call_sid),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def set_summary(call_sid: str, summary: str) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute(
            "UPDATE calls SET summary = %s WHERE call_sid = %s RETURNING *",
            (summary, call_sid),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def count_active_calls(phone_number_id: str, org_id: str) -> int:
    """
    Return the number of active (initiated or in-progress) calls for a phone number.

    Used by POST /call/outbound to enforce max_concurrent_calls before reserving a
    worker. Live count from the calls table — no denormalized counter to keep in sync.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM   calls
            WHERE  phone_number_id = %s
              AND  org_id          = %s
              AND  status IN ('initiated', 'in-progress')
            """,
            (phone_number_id, org_id),
        )
        return cur.fetchone()["count"]


def count_calls_today(phone_number_id: str, org_id: str) -> int:
    """
    Return the number of calls initiated today (UTC calendar day) for a phone number.

    Used by POST /call/outbound to enforce max_calls_per_day. Live count — no counter.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM   calls
            WHERE  phone_number_id = %s
              AND  org_id          = %s
              AND  DATE(created_at AT TIME ZONE 'UTC') = CURRENT_DATE
            """,
            (phone_number_id, org_id),
        )
        return cur.fetchone()["count"]


_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "busy",
    "no-answer",
    "canceled",
    "cancelled",
}


async def sync_call_request_outcome(
    call_id: str,
    call_status: str,
    duration_seconds: Optional[int] = None,
    call_start_time: Optional[datetime] = None,
    call_end_time: Optional[datetime] = None,
    recording_url: Optional[str] = None,
) -> None:
    """
    Best-effort sync of call status to call_requests — fires on every status update.

    Terminal statuses: updates lifecycle status, timing, duration, and recording_url.
    Non-terminal statuses (e.g. in-progress, ringing): updates call_status and
    call_start_time only so the record stays in sync mid-call.

    Never raises — failures are logged and swallowed so the provider webhook
    always receives a 200 OK response.
    """
    from loguru import logger

    is_terminal = call_status in _TERMINAL_STATUSES

    try:
        if is_terminal:
            # Normalize provider status values:
            #   cr_status        → call_requests.status  (lifecycle; CHECK constraint uses 'cancelled')
            #   call_status_norm → call_requests.call_status  (normalized; also used for
            #                      webhook enqueue after the UPDATE)
            status_map = {
                "completed": "completed",
                "failed": "failed",
                "busy": "busy",
                "no-answer": "no-answer",
                "canceled": "cancelled",  # Twilio spells it without double-l
                "cancelled": "cancelled",
            }
            cr_status = status_map.get(call_status, "failed")
            call_status_norm = status_map.get(call_status, call_status)

            fields = ["status = %s", "call_status = %s", "updated_at = NOW()"]
            params: list = [cr_status, call_status_norm]

            if duration_seconds is not None:
                fields.append("call_duration_seconds = %s")
                fields.append("call_duration_minutes = %s")
                params += [duration_seconds, round(duration_seconds / 60.0, 2)]
            if call_start_time is not None:
                fields.append("call_start_time = %s")
                params.append(call_start_time)
            if call_end_time is not None:
                fields.append("call_end_time = %s")
                params.append(call_end_time)
            if recording_url is not None:
                fields.append("recording_url = COALESCE(%s, recording_url)")
                params.append(recording_url)

            params.append(call_id)
            with get_cursor() as cur:
                cur.execute(
                    f"UPDATE call_requests SET {', '.join(fields)} WHERE id = %s RETURNING org_id",
                    params,
                )
                updated_row = cur.fetchone()
            logger.info(
                f"sync_call_request_outcome: updated call_requests id={call_id} status={cr_status}"
            )

            # Enqueue webhook delivery then trigger if data is ready (best-effort)
            if updated_row:
                try:
                    from app.services.webhook_service import (
                        enqueue_webhook,
                        trigger_pending_webhooks,
                    )

                    enqueue_webhook(
                        call_request_id=call_id,
                        org_id=str(updated_row["org_id"]),
                        call_status=call_status_norm,
                    )

                    # Trigger immediately when recording_url is already available
                    # OR when no recording is expected (non-completed statuses).
                    # For completed calls with no recording_url yet, the poller
                    # will fire after recording-status callback triggers us again.
                    if recording_url is not None or call_status_norm != "completed":
                        trigger_pending_webhooks(call_id)
                except Exception as wh_exc:
                    logger.warning(
                        f"sync_call_request_outcome: webhook enqueue/trigger failed: {wh_exc}"
                    )
        else:
            # Non-terminal: keep call_status in sync mid-call (no lifecycle/timing changes)
            fields = ["call_status = %s", "updated_at = NOW()"]
            params = [call_status]
            if call_start_time is not None:
                fields.append("call_start_time = %s")
                params.append(call_start_time)
            params.append(call_id)
            with get_cursor() as cur:
                cur.execute(
                    f"UPDATE call_requests SET {', '.join(fields)} WHERE id = %s",
                    params,
                )
            logger.debug(
                f"sync_call_request_outcome: mid-call update id={call_id} call_status={call_status}"
            )
    except Exception as exc:
        logger.error(f"sync_call_request_outcome: failed for call_id={call_id}: {exc}")


_STATUS_MAP = {
    "completed": "answered",
    "no-answer": "missed",
    "busy": "busy",
    "failed": "failed",
    "cancelled": "rejected",
    "canceled": "rejected",
}


def _normalize_status(raw: Optional[str]) -> str:
    return _STATUS_MAP.get(raw or "", raw or "unknown")


def _row_to_stat(row: dict) -> dict:
    # Build E.164 to_number from stored phone_number
    phone = row.get("phone_number") or ""
    to_number = phone if phone.startswith("+") else f"+91{phone}" if phone else None

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
        "call_status": _normalize_status(row.get("call_status") or row.get("status")),
        "call_direction": row.get("call_direction", "outbound"),
        "call_start_time": row["call_start_time"].isoformat()
        if row.get("call_start_time")
        else None,
        "call_end_time": row["call_end_time"].isoformat()
        if row.get("call_end_time")
        else None,
        "total_duration_seconds": row.get("call_duration_seconds"),
        "recording_url": presign_recording_url(row.get("recording_url")),
        "initiation_payload": initiation_payload,
    }


def get_call_stat(request_id: str, org_id: str) -> Optional[dict]:
    """Return stats for a single call request by its ID."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, call_status, status, call_direction,
                   call_start_time, call_end_time,
                   call_duration_seconds, recording_url,
                   bot_id, phone_number_id, phone_number,
                   lead_id, campaign_id,
                   custom_params, additional_data,
                   callback_url, scheduled_at, priority
            FROM call_requests
            WHERE id = %s AND org_id = %s
            """,
            (request_id, org_id),
        )
        row = cur.fetchone()
        return _row_to_stat(dict(row)) if row else None


def get_call_stats(
    org_id: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    call_status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list:
    """Return a list of per-call stat records for an org, optionally filtered."""
    conditions = ["org_id = %s"]
    params: list = [org_id]

    if from_date:
        conditions.append("created_at >= %s")
        params.append(from_date)
    if to_date:
        conditions.append("created_at <= %s")
        params.append(to_date)
    # Filter by PRD status values — map back to internal before querying
    reverse_map = {
        "answered": "completed",
        "missed": "no-answer",
        "rejected": "cancelled",
    }
    if call_status:
        internal = reverse_map.get(call_status, call_status)
        conditions.append("(call_status = %s OR status = %s)")
        params.extend([internal, internal])
    # Only return terminal calls — exclude queued/processing/initiated
    conditions.append("status NOT IN ('queued', 'processing', 'initiated')")

    where = "WHERE " + " AND ".join(conditions)
    params.extend([limit, offset])

    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, call_status, status, call_direction,
                   call_start_time, call_end_time,
                   call_duration_seconds, recording_url,
                   bot_id, phone_number_id, phone_number,
                   lead_id, campaign_id,
                   custom_params, additional_data,
                   callback_url, scheduled_at, priority
            FROM call_requests
            {where}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            params,
        )
        return [_row_to_stat(dict(r)) for r in cur.fetchall()]


def get_webhook_deliveries(request_id: str, org_id: str) -> list:
    """Return all webhook delivery attempts for a call-stat, scoped to the org."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT wd.id, wd.call_request_id, wd.event_type, wd.webhook_url,
                   wd.status, wd.attempt_number, wd.max_attempts,
                   wd.response_status_code, wd.response_time_ms,
                   wd.error_message, wd.created_at, wd.last_attempted_at,
                   wd.delivered_at, wd.next_retry_at
            FROM webhook_deliveries wd
            JOIN call_requests cr ON cr.id = wd.call_request_id
            WHERE wd.call_request_id = %s
              AND cr.org_id = %s
            ORDER BY wd.created_at ASC
            """,
            (request_id, org_id),
        )
        rows = cur.fetchall()

    def _row_to_delivery(row: dict) -> dict:
        return {
            "id": str(row["id"]),
            "call_request_id": str(row["call_request_id"]),
            "event_type": row["event_type"],
            "webhook_url": row["webhook_url"],
            "status": row["status"],
            "attempt_number": row["attempt_number"],
            "max_attempts": row["max_attempts"],
            "response_status_code": row.get("response_status_code"),
            "response_time_ms": row.get("response_time_ms"),
            "error_message": row.get("error_message"),
            "created_at": row["created_at"].isoformat()
            if row.get("created_at")
            else None,
            "last_attempted_at": row["last_attempted_at"].isoformat()
            if row.get("last_attempted_at")
            else None,
            "delivered_at": row["delivered_at"].isoformat()
            if row.get("delivered_at")
            else None,
            "next_retry_at": row["next_retry_at"].isoformat()
            if row.get("next_retry_at")
            else None,
        }

    return [_row_to_delivery(dict(r)) for r in rows]


def save_metrics(call_sid: str, metrics: dict) -> None:
    """Persist per-call performance metrics to calls.metrics JSONB column."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE calls SET metrics = %s WHERE call_sid = %s",
            (Json(metrics), call_sid),
        )


def get_many(
    org_id: Optional[str] = None,
    phone_number_id: Optional[str] = None,
    assistant_id: Optional[str] = None,
    status: Optional[str] = None,
    direction: Optional[str] = None,
    from_number: Optional[str] = None,
    to_number: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list:
    conditions: list[str] = []
    params: list = []

    if org_id:
        conditions.append("org_id = %s")
        params.append(org_id)
    if phone_number_id:
        conditions.append("phone_number_id = %s")
        params.append(phone_number_id)
    if assistant_id:
        conditions.append("assistant_id = %s")
        params.append(assistant_id)
    if status:
        conditions.append("status = %s")
        params.append(status)
    if direction:
        conditions.append("direction = %s")
        params.append(direction)
    if from_number:
        conditions.append("from_number = %s")
        params.append(from_number)
    if to_number:
        conditions.append("to_number = %s")
        params.append(to_number)

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.extend([limit, offset])

    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT * FROM calls
            {where_clause}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """,
            params,
        )
        return [dict(row) for row in cur.fetchall()]
