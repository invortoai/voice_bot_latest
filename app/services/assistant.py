from typing import Optional

from psycopg2.extras import Json

from app.core.database import get_cursor
from app.models.schemas import DEFAULT_LLM_SETTINGS


def create(
    name: str,
    system_prompt: str,
    org_id: Optional[str] = None,
    description: Optional[str] = None,
    llm_provider: str = "openai",
    model: str = "gpt-4.1-nano",
    llm_settings: Optional[dict] = None,
    voice_provider: str = "elevenlabs",
    voice_id: Optional[str] = None,
    voice_model: str = "eleven_flash_v2_5",
    voice_settings: Optional[dict] = None,
    greeting_message: Optional[str] = None,
    end_call_phrases: Optional[list] = None,
    transcriber_provider: str = "deepgram",
    transcriber_model: str = "nova-2",
    transcriber_language: str = "en",
    transcriber_settings: Optional[dict] = None,
    vad_settings: Optional[dict] = None,
    interruption_strategy: Optional[str] = None,
    insight_enabled: bool = False,
    insights_config_id: Optional[str] = None,
    knowledge_base_id: Optional[str] = None,
    rag_top_k: int = 5,
    rag_score_threshold: float = 0.35,
    rag_context_query: Optional[str] = None,
) -> dict:
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO assistants (
                org_id, name, description, system_prompt, llm_provider, model, llm_settings,
                voice_provider, voice_id, voice_model, voice_settings, greeting_message, end_call_phrases,
                transcriber_provider, transcriber_model, transcriber_language, transcriber_settings,
                vad_settings, interruption_strategy, insight_enabled, insights_config_id,
                knowledge_base_id, rag_top_k, rag_score_threshold, rag_context_query
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """,
            (
                org_id,
                name,
                description,
                system_prompt,
                llm_provider,
                model,
                Json(llm_settings or dict(DEFAULT_LLM_SETTINGS)),
                voice_provider,
                voice_id,
                voice_model,
                Json(voice_settings or {}),
                greeting_message,
                end_call_phrases,
                transcriber_provider,
                transcriber_model,
                transcriber_language,
                Json(transcriber_settings or {}),
                Json(vad_settings or {}),
                interruption_strategy or "default",
                insight_enabled,
                insights_config_id,
                knowledge_base_id,
                rag_top_k,
                rag_score_threshold,
                rag_context_query,
            ),
        )
        return dict(cur.fetchone())


def get_by_id(assistant_id: str, org_id: Optional[str] = None) -> Optional[dict]:
    with get_cursor() as cur:
        if org_id:
            cur.execute(
                "SELECT * FROM assistants WHERE id = %s AND org_id = %s",
                (assistant_id, org_id),
            )
        else:
            cur.execute("SELECT * FROM assistants WHERE id = %s", (assistant_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_active(org_id: Optional[str] = None) -> list:
    with get_cursor() as cur:
        if org_id:
            cur.execute(
                "SELECT * FROM assistants WHERE is_active = true AND org_id = %s ORDER BY name",
                (org_id,),
            )
        else:
            cur.execute("SELECT * FROM assistants WHERE is_active = true ORDER BY name")
        return [dict(row) for row in cur.fetchall()]


def update(assistant_id: str, org_id: Optional[str] = None, **kwargs) -> Optional[dict]:
    if not kwargs:
        return get_by_id(assistant_id, org_id=org_id)

    set_clauses = []
    values = []
    for key, value in kwargs.items():
        if key in (
            "llm_settings",
            "voice_settings",
            "transcriber_settings",
            "vad_settings",
        ) and isinstance(value, dict):
            set_clauses.append(f"{key} = %s")
            values.append(Json(value))
        else:
            set_clauses.append(f"{key} = %s")
            values.append(value)

    if org_id:
        values.extend([assistant_id, org_id])
        where = "WHERE id = %s AND org_id = %s"
    else:
        values.append(assistant_id)
        where = "WHERE id = %s"

    with get_cursor() as cur:
        cur.execute(
            f"""
            UPDATE assistants
            SET {", ".join(set_clauses)}
            {where}
            RETURNING *
        """,
            values,
        )
        row = cur.fetchone()
        return dict(row) if row else None


def delete(assistant_id: str, org_id: Optional[str] = None) -> bool:
    with get_cursor() as cur:
        if org_id:
            cur.execute(
                "DELETE FROM assistants WHERE id = %s AND org_id = %s RETURNING id",
                (assistant_id, org_id),
            )
        else:
            cur.execute(
                "DELETE FROM assistants WHERE id = %s RETURNING id", (assistant_id,)
            )
        return cur.fetchone() is not None
