"""CRUD for insights_config."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import psycopg2
import psycopg2.errors
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.auth import verify_global_key_with_org
from app.core.database import get_cursor
from app.models.insights_config import InsightsConfig
from app.services.insights_config_repository import InsightsConfigRepository
from app.utils.exceptions import handle_db_errors

router = APIRouter(prefix="/insights/config", tags=["Insights: Config"])


def get_config_repo() -> InsightsConfigRepository:
    return InsightsConfigRepository()


def _check_org(config: InsightsConfig, org_id: str) -> None:
    """Raise 404 if config does not belong to the requesting org."""
    if str(config.org_id) != org_id:
        raise HTTPException(status_code=404, detail="Config not found")


class ConfigCreateRequest(BaseModel):
    name: str = "default"
    is_default: bool = False
    stt_provider: str = "deepgram"
    stt_model: str = "nova-2"
    stt_language: str = "en"
    stt_speaker_index_bot: int = 0
    stt_multichannel: bool = False
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-4-20250514"
    llm_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
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


class ConfigUpdateRequest(BaseModel):
    name: str | None = None
    stt_provider: str | None = None
    stt_model: str | None = None
    stt_language: str | None = None
    stt_speaker_index_bot: int | None = None
    stt_multichannel: bool | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    analysis_prompt: str | None = None
    enable_summary: bool | None = None
    enable_sentiment: bool | None = None
    enable_key_topics: bool | None = None
    enable_call_score: bool | None = None
    enable_call_outcome: bool | None = None
    enable_actionable_insights: bool | None = None
    allowed_call_outcomes: list[str] | None = None
    custom_fields_schema: dict[str, Any] | None = None
    callback_url: str | None = None
    callback_secret: str | None = None
    force_worker_audio_download: bool | None = None
    is_default: bool = False


@router.post("", response_model=InsightsConfig, status_code=201)
def create_config(
    body: ConfigCreateRequest,
    org_ctx: dict = Depends(verify_global_key_with_org),
    repo: InsightsConfigRepository = Depends(get_config_repo),
) -> InsightsConfig:
    org_id = org_ctx["org_id"]
    with handle_db_errors("create config"):
        if body.is_default:
            repo.unset_default_for_org(org_id)
        config_id = repo.create(
            org_id=org_id,
            name=body.name,
            is_default=body.is_default,
            stt_provider=body.stt_provider,
            stt_model=body.stt_model,
            stt_language=body.stt_language,
            stt_speaker_index_bot=body.stt_speaker_index_bot,
            stt_multichannel=body.stt_multichannel,
            llm_provider=body.llm_provider,
            llm_model=body.llm_model,
            llm_temperature=Decimal(str(body.llm_temperature)),
            analysis_prompt=body.analysis_prompt,
            enable_summary=body.enable_summary,
            enable_sentiment=body.enable_sentiment,
            enable_key_topics=body.enable_key_topics,
            enable_call_score=body.enable_call_score,
            enable_call_outcome=body.enable_call_outcome,
            enable_actionable_insights=body.enable_actionable_insights,
            allowed_call_outcomes=body.allowed_call_outcomes,
            custom_fields_schema=body.custom_fields_schema,
            callback_url=body.callback_url,
            callback_secret=body.callback_secret,
            force_worker_audio_download=body.force_worker_audio_download,
        )

    config = repo.find_by_id(config_id)
    if config is None:
        raise HTTPException(
            status_code=500, detail="Config created but could not be fetched"
        )
    return config


@router.get("", response_model=list[InsightsConfig])
def list_configs(
    org_ctx: dict = Depends(verify_global_key_with_org),
    repo: InsightsConfigRepository = Depends(get_config_repo),
) -> list[InsightsConfig]:
    return repo.find_by_org(org_ctx["org_id"])


@router.get("/{config_id}", response_model=InsightsConfig)
def get_config(
    config_id: uuid.UUID,
    org_ctx: dict = Depends(verify_global_key_with_org),
    repo: InsightsConfigRepository = Depends(get_config_repo),
) -> InsightsConfig:
    config = repo.find_by_id(config_id)
    if config is None:
        raise HTTPException(status_code=404, detail="Config not found")
    _check_org(config, org_ctx["org_id"])
    return config


@router.put("/{config_id}", response_model=InsightsConfig)
def update_config(
    config_id: uuid.UUID,
    body: ConfigUpdateRequest,
    org_ctx: dict = Depends(verify_global_key_with_org),
    repo: InsightsConfigRepository = Depends(get_config_repo),
) -> InsightsConfig:
    existing = repo.find_by_id(config_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Config not found")
    _check_org(existing, org_ctx["org_id"])
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return existing
    if "llm_temperature" in updates:
        updates["llm_temperature"] = Decimal(str(updates["llm_temperature"]))
    with handle_db_errors("update config"):
        if updates.get("is_default"):
            repo.unset_default_for_org(str(existing.org_id))
        repo.update(config_id, **updates)
    return repo.find_by_id(config_id)


@router.delete("/{config_id}", status_code=204)
def delete_config(
    config_id: uuid.UUID,
    org_ctx: dict = Depends(verify_global_key_with_org),
    repo: InsightsConfigRepository = Depends(get_config_repo),
) -> Response:
    existing = repo.find_by_id(config_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Config not found")
    _check_org(existing, org_ctx["org_id"])

    # Block deletion if any assistant references this config
    with get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM assistants WHERE insights_config_id = %s LIMIT 1",
            (str(config_id),),
        )
        if cur.fetchone():
            raise HTTPException(
                status_code=409,
                detail="Config is referenced by one or more assistants and cannot be deleted",
            )

    try:
        repo.delete(config_id)
    except psycopg2.errors.ForeignKeyViolation:
        raise HTTPException(
            status_code=409,
            detail="Config is referenced by existing analyses and cannot be deleted",
        )
    except psycopg2.Error as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    return Response(status_code=204)
