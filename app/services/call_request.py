"""
call_request.py — Service for the customer-facing call initiation API.

Operates on the call_requests table (shared Supabase DB).
Distinct from call.py which operates on the runner-internal calls table.
"""

import re
from typing import Optional

from psycopg2.extras import Json

from app.core.database import get_cursor


_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_ACTIVE_STATUSES = ("queued", "processing", "initiated")


def _validate(
    to_number: str,
    callback_url: Optional[str],
    input_variables: Optional[dict],
) -> None:
    if not _E164_RE.match(to_number):
        raise ValueError(
            f"to_number must be in E.164 format (e.g. +917022xxxxxx), got: {to_number}"
        )

    if callback_url and not callback_url.startswith("https://"):
        raise ValueError("callback_url must be a valid HTTPS URL")

    if input_variables:
        if len(input_variables) > 20:
            raise ValueError("input_variables must not exceed 20 key-value pairs")
        for k, v in input_variables.items():
            if not isinstance(v, str):
                raise ValueError(
                    f"input_variables values must be strings, got {type(v).__name__} for key '{k}'"
                )
            if len(v) > 500:
                raise ValueError(
                    f"input_variables value for '{k}' exceeds 500 characters"
                )


def check_duplicate(to_number: str, org_id: str, campaign_id: Optional[str]) -> bool:
    """
    Return True if this to_number already has an active request.
    - With campaign_id: scoped to that campaign.
    - Without campaign_id: org-wide check for source='api' calls.
    """
    clean_number = to_number.lstrip("+").replace(" ", "")
    with get_cursor() as cur:
        if campaign_id:
            cur.execute(
                """
                SELECT 1 FROM call_requests
                WHERE org_id      = %s
                  AND campaign_id  = %s
                  AND phone_number = %s
                  AND status IN ('queued', 'processing', 'initiated')
                LIMIT 1
                """,
                (org_id, campaign_id, clean_number),
            )
        else:
            cur.execute(
                """
                SELECT 1 FROM call_requests
                WHERE org_id      = %s
                  AND source       = 'api'
                  AND phone_number = %s
                  AND status IN ('queued', 'processing', 'initiated')
                LIMIT 1
                """,
                (org_id, clean_number),
            )
        return cur.fetchone() is not None


def create(
    org_id: str,
    assistant_id: str,
    phone_number_id: str,
    to_number: str,
    input_variables: Optional[dict] = None,
    external_customer_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
    callback_url: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    priority: int = 5,
    additional_data: Optional[dict] = None,
) -> dict:
    """
    Insert a new call_requests row and return it.

    custom_params merges input_variables + to_number so all
    are available as {{variable_name}} tokens in the conversation script.
    """
    _validate(to_number, callback_url, input_variables)

    # Build custom_params — everything injectable into the conversation script
    custom_params: dict = {}
    if input_variables:
        custom_params.update(input_variables)
    custom_params["to_number"] = to_number

    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO call_requests (
                org_id, source, campaign_id,
                phone_number, lead_id,
                bot_id, phone_number_id,
                custom_params, additional_data,
                callback_url, scheduled_at, priority,
                status
            ) VALUES (
                %s, 'api', %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s,
                'queued'
            )
            RETURNING id, status, created_at
            """,
            (
                org_id,
                campaign_id,
                to_number.lstrip("+"),  # stored digits-only per table design
                external_customer_id,
                assistant_id,
                phone_number_id,
                Json(custom_params),
                Json(additional_data or {}),
                callback_url,
                scheduled_at,
                priority,
            ),
        )
        return dict(cur.fetchone())
