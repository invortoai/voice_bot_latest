from typing import Optional
from psycopg2.extras import Json

from app.core.database import get_cursor


def create(
    phone_number: str,
    org_id: Optional[str] = None,
    friendly_name: Optional[str] = None,
    provider: str = "twilio",
    provider_credentials: Optional[dict] = None,
    assistant_id: Optional[str] = None,
    is_inbound_enabled: bool = True,
    is_outbound_enabled: bool = True,
    max_call_duration_seconds: int = 3600,
) -> dict:
    """Create a new phone number configuration."""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO phone_numbers (
                org_id, phone_number, friendly_name, provider, provider_credentials,
                assistant_id, is_inbound_enabled, is_outbound_enabled, max_call_duration_seconds
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """,
            (
                org_id,
                phone_number,
                friendly_name,
                provider,
                Json(provider_credentials or {}),
                assistant_id,
                is_inbound_enabled,
                is_outbound_enabled,
                max_call_duration_seconds,
            ),
        )
        return dict(cur.fetchone())


def get_by_number(phone_number: str) -> Optional[dict]:
    """Look up by E.164 number — used by webhooks (no org scoping needed)."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT pn.*,
                   a.id as assistant_id,
                   a.name as assistant_name,
                   a.system_prompt,
                   a.greeting_message,
                   a.llm_provider,
                   a.model,
                   a.llm_settings,
                   a.voice_provider,
                   a.voice_id,
                   a.voice_settings,
                   a.end_call_phrases,
                   a.transcriber_provider,
                   a.transcriber_model,
                   a.transcriber_language,
                   a.transcriber_settings
            FROM phone_numbers pn
            LEFT JOIN assistants a ON pn.assistant_id = a.id
            WHERE pn.phone_number = %s AND pn.is_active = true
        """,
            (phone_number,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_by_id(phone_number_id: str, org_id: Optional[str] = None) -> Optional[dict]:
    _join = """
        SELECT pn.*,
               a.id as assistant_id,
               a.name as assistant_name,
               a.system_prompt,
               a.greeting_message,
               a.llm_provider,
               a.model,
               a.llm_settings,
               a.voice_provider,
               a.voice_id,
               a.voice_settings,
               a.end_call_phrases,
               a.transcriber_provider,
               a.transcriber_model,
               a.transcriber_language,
               a.transcriber_settings
        FROM phone_numbers pn
        LEFT JOIN assistants a ON pn.assistant_id = a.id
    """
    with get_cursor() as cur:
        if org_id:
            cur.execute(
                _join + "WHERE pn.id = %s AND pn.org_id = %s", (phone_number_id, org_id)
            )
        else:
            cur.execute(_join + "WHERE pn.id = %s", (phone_number_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_active(org_id: Optional[str] = None) -> list:
    with get_cursor() as cur:
        if org_id:
            cur.execute(
                """
                SELECT pn.*, a.name as assistant_name
                FROM phone_numbers pn
                LEFT JOIN assistants a ON pn.assistant_id = a.id
                WHERE pn.is_active = true AND pn.org_id = %s
                ORDER BY pn.phone_number
                """,
                (org_id,),
            )
        else:
            cur.execute("""
                SELECT pn.*, a.name as assistant_name
                FROM phone_numbers pn
                LEFT JOIN assistants a ON pn.assistant_id = a.id
                WHERE pn.is_active = true
                ORDER BY pn.phone_number
            """)
        return [dict(row) for row in cur.fetchall()]


def update(
    phone_number_id: str, org_id: Optional[str] = None, **kwargs
) -> Optional[dict]:
    if not kwargs:
        return get_by_id(phone_number_id, org_id=org_id)

    set_clauses = []
    values = []
    for key, value in kwargs.items():
        set_clauses.append(f"{key} = %s")
        if key == "provider_credentials" and isinstance(value, dict):
            values.append(Json(value))
        else:
            values.append(value)

    if org_id:
        values.extend([phone_number_id, org_id])
        where = "WHERE id = %s AND org_id = %s"
    else:
        values.append(phone_number_id)
        where = "WHERE id = %s"

    with get_cursor() as cur:
        cur.execute(
            f"UPDATE phone_numbers SET {', '.join(set_clauses)} {where} RETURNING *",
            values,
        )
        row = cur.fetchone()
        return dict(row) if row else None


def delete(phone_number_id: str, org_id: Optional[str] = None) -> bool:
    with get_cursor() as cur:
        if org_id:
            cur.execute(
                "DELETE FROM phone_numbers WHERE id = %s AND org_id = %s RETURNING id",
                (phone_number_id, org_id),
            )
        else:
            cur.execute(
                "DELETE FROM phone_numbers WHERE id = %s RETURNING id",
                (phone_number_id,),
            )
        return cur.fetchone() is not None


def assign_assistant(phone_number: str, assistant_id: str) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute(
            "UPDATE phone_numbers SET assistant_id = %s WHERE phone_number = %s RETURNING *",
            (assistant_id, phone_number),
        )
        row = cur.fetchone()
        return dict(row) if row else None
