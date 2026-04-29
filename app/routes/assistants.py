import uuid

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.core.auth import verify_global_key_with_org
from app.core.database import get_cursor
from app.models.schemas import AssistantCreate, AssistantUpdate
from app.services import assistant_service

router = APIRouter(prefix="/assistants", tags=["Assistants"])


def _validate_insights_config(config_id: uuid.UUID, org_id: str) -> None:
    """Raise 422 if insights_config_id does not exist for this org."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM insights_config WHERE id = %s AND org_id = %s",
            (str(config_id), org_id),
        )
        if not cur.fetchone():
            raise HTTPException(
                status_code=422,
                detail="insights_config not found for this org",
            )


def _check_org_has_any_insights_config(org_id: str) -> None:
    """Raise 422 if the org has no insights_config at all."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM insights_config WHERE org_id = %s LIMIT 1",
            (org_id,),
        )
        if not cur.fetchone():
            raise HTTPException(
                status_code=422,
                detail="No insights_config found for this org. Create one first or provide insights_config_id.",
            )


@router.post("")
async def create_assistant(
    request: AssistantCreate,
    org_ctx: dict = Depends(verify_global_key_with_org),
):
    try:
        org_id = org_ctx["org_id"]

        if request.insights_config_id is not None:
            _validate_insights_config(request.insights_config_id, org_id)
        elif request.insight_enabled:
            _check_org_has_any_insights_config(org_id)

        data = request.model_dump(exclude_none=True)
        if "insights_config_id" in data:
            data["insights_config_id"] = str(data["insights_config_id"])
        data["org_id"] = org_ctx["org_id"]
        assistant = assistant_service.create(**data)
        return assistant
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating assistant: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
async def list_assistants(org_ctx: dict = Depends(verify_global_key_with_org)):
    try:
        assistants = assistant_service.get_active(org_id=org_ctx["org_id"])
        return {"assistants": assistants, "total": len(assistants)}
    except Exception as e:
        logger.error(f"Error listing assistants: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{assistant_id}")
async def get_assistant(
    assistant_id: str,
    org_ctx: dict = Depends(verify_global_key_with_org),
):
    try:
        assistant = assistant_service.get_by_id(assistant_id, org_id=org_ctx["org_id"])
        if not assistant:
            raise HTTPException(status_code=404, detail="Assistant not found")
        return assistant
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting assistant: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{assistant_id}")
async def update_assistant(
    assistant_id: str,
    request: AssistantUpdate,
    org_ctx: dict = Depends(verify_global_key_with_org),
):
    try:
        org_id = org_ctx["org_id"]
        updates = request.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No updates provided")

        if "insights_config_id" in updates:
            _validate_insights_config(updates["insights_config_id"], org_id)
            updates["insights_config_id"] = str(updates["insights_config_id"])

        # Enabling insights with no config_id in this payload — check assistant
        # already has one stored, otherwise require org to have at least one config
        if (
            updates.get("insight_enabled") is True
            and "insights_config_id" not in updates
        ):
            existing = assistant_service.get_by_id(assistant_id, org_id=org_id)
            if not existing:
                raise HTTPException(status_code=404, detail="Assistant not found")
            if not existing.get("insights_config_id"):
                _check_org_has_any_insights_config(org_id)

        assistant = assistant_service.update(
            assistant_id, org_id=org_ctx["org_id"], **updates
        )
        if not assistant:
            raise HTTPException(status_code=404, detail="Assistant not found")
        return assistant
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating assistant: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{assistant_id}")
async def delete_assistant(
    assistant_id: str,
    org_ctx: dict = Depends(verify_global_key_with_org),
):
    try:
        deleted = assistant_service.delete(assistant_id, org_id=org_ctx["org_id"])
        if not deleted:
            raise HTTPException(status_code=404, detail="Assistant not found")
        return {"status": "deleted", "id": assistant_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting assistant: {e}")
        raise HTTPException(status_code=500, detail=str(e))
