"""Repository for the insights_config table."""

from __future__ import annotations

import uuid
from typing import Any

from psycopg2.extras import Json

from app.core.database import get_cursor
from app.models.insights_config import InsightsConfig


class InsightsConfigRepository:
    def create(
        self, *, org_id: uuid.UUID, name: str = "default", **fields: Any
    ) -> uuid.UUID:
        allowed = {
            "is_default",
            "stt_provider",
            "stt_model",
            "stt_language",
            "stt_speaker_index_bot",
            "stt_multichannel",
            "llm_provider",
            "llm_model",
            "llm_temperature",
            "analysis_prompt",
            "enable_summary",
            "enable_sentiment",
            "enable_key_topics",
            "enable_call_score",
            "enable_call_outcome",
            "enable_actionable_insights",
            "allowed_call_outcomes",
            "custom_fields_schema",
            "callback_url",
            "callback_secret",
            "force_worker_audio_download",
        }
        extra = {k: v for k, v in fields.items() if k in allowed and v is not None}
        # Wrap JSONB fields
        if extra.get("custom_fields_schema") is not None:
            extra["custom_fields_schema"] = Json(extra["custom_fields_schema"])
        columns = ["org_id", "name"] + list(extra.keys())
        placeholders = ["%s"] * len(columns)
        values = [str(org_id), name] + list(extra.values())
        with get_cursor() as cur:
            cur.execute(
                f"INSERT INTO insights_config ({', '.join(columns)}) VALUES ({', '.join(placeholders)}) RETURNING id",
                values,
            )
            return cur.fetchone()["id"]

    def unset_default_for_org(self, org_id: str) -> None:
        with get_cursor() as cur:
            cur.execute(
                "UPDATE insights_config SET is_default = FALSE WHERE org_id = %s AND is_default = TRUE",
                (str(org_id),),
            )

    def update(self, config_id: uuid.UUID, **fields: Any) -> bool:
        allowed = {
            "name",
            "is_default",
            "stt_provider",
            "stt_model",
            "stt_language",
            "stt_speaker_index_bot",
            "stt_multichannel",
            "llm_provider",
            "llm_model",
            "llm_temperature",
            "analysis_prompt",
            "enable_summary",
            "enable_sentiment",
            "enable_key_topics",
            "enable_call_score",
            "enable_call_outcome",
            "enable_actionable_insights",
            "allowed_call_outcomes",
            "custom_fields_schema",
            "callback_url",
            "callback_secret",
            "force_worker_audio_download",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        if updates.get("custom_fields_schema") is not None:
            updates["custom_fields_schema"] = Json(updates["custom_fields_schema"])
        set_clauses = [f"{col} = %s" for col in updates.keys()]
        values = list(updates.values()) + [str(config_id)]
        with get_cursor() as cur:
            cur.execute(
                f"UPDATE insights_config SET {', '.join(set_clauses)}, updated_at = NOW() WHERE id = %s",
                values,
            )
            return cur.rowcount == 1

    def delete(self, config_id: uuid.UUID) -> bool:
        with get_cursor() as cur:
            cur.execute("DELETE FROM insights_config WHERE id = %s", (str(config_id),))
            return cur.rowcount == 1

    def find_by_id(self, config_id: uuid.UUID) -> InsightsConfig | None:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id, org_id, name, is_default,
                       stt_provider, stt_model, stt_language, stt_speaker_index_bot, stt_multichannel,
                       llm_provider, llm_model, llm_temperature, analysis_prompt,
                       enable_summary, enable_sentiment, enable_key_topics,
                       enable_call_score, enable_call_outcome,
                       enable_actionable_insights,
                       allowed_call_outcomes, custom_fields_schema,
                       callback_url, callback_secret,
                       force_worker_audio_download, created_at, updated_at
                FROM insights_config WHERE id = %s
                """,
                (str(config_id),),
            )
            row = cur.fetchone()
        return _row_to_config(dict(row)) if row else None

    def find_by_org(self, org_id: uuid.UUID) -> list[InsightsConfig]:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id, org_id, name, is_default,
                       stt_provider, stt_model, stt_language, stt_speaker_index_bot, stt_multichannel,
                       llm_provider, llm_model, llm_temperature, analysis_prompt,
                       enable_summary, enable_sentiment, enable_key_topics,
                       enable_call_score, enable_call_outcome,
                       enable_actionable_insights,
                       allowed_call_outcomes, custom_fields_schema,
                       callback_url, callback_secret,
                       force_worker_audio_download, created_at, updated_at
                FROM insights_config WHERE org_id = %s ORDER BY created_at ASC
                """,
                (str(org_id),),
            )
            rows = cur.fetchall()
        return [_row_to_config(dict(r)) for r in rows]


def _row_to_config(data: dict) -> InsightsConfig:
    import json

    if data.get("allowed_call_outcomes") is None:
        data["allowed_call_outcomes"] = []
    if isinstance(data.get("custom_fields_schema"), str):
        try:
            data["custom_fields_schema"] = json.loads(data["custom_fields_schema"])
        except (json.JSONDecodeError, TypeError):
            data["custom_fields_schema"] = None
    return InsightsConfig(**data)
