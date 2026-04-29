"""InsightsConfig — per-org AI configuration from the insights_config table."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class InsightsConfig(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str = "default"
    is_default: bool = False

    stt_provider: str = "deepgram"
    stt_model: str = "nova-2"
    stt_language: str = "en"
    stt_speaker_index_bot: int = 0
    stt_multichannel: bool = False

    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-4-20250514"
    llm_temperature: Decimal = Decimal("0.10")
    analysis_prompt: str | None = None

    enable_summary: bool = True
    enable_sentiment: bool = True
    enable_key_topics: bool = True
    enable_call_score: bool = True
    enable_call_outcome: bool = True
    enable_actionable_insights: bool = True

    allowed_call_outcomes: list[str] = Field(
        default_factory=lambda: [
            "appointment_booked",
            "interested",
            "not_interested",
            "callback_requested",
            "do_not_call",
            "unresolved",
        ]
    )

    custom_fields_schema: dict[str, Any] | None = None

    callback_url: str | None = None
    callback_secret: str | None = None

    force_worker_audio_download: bool = False

    created_at: datetime | None = None
    updated_at: datetime | None = None
