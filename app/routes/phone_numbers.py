from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.core.auth import verify_global_key_with_org
from app.models.schemas import PhoneNumberCreate, PhoneNumberUpdate
from app.services import phone_number_service

router = APIRouter(prefix="/phone-numbers", tags=["Phone Numbers"])

_SENSITIVE_CRED_KEYS = {"auth_token", "secret_access_key", "api_key", "token"}
_INTERNAL_FIELDS = ("system_prompt", "greeting_message")


def _sanitize_phone_response(phone: dict) -> dict:
    """Remove sensitive fields from phone number API responses."""
    if not phone:
        return phone
    result = dict(phone)
    creds = result.get("provider_credentials")
    if isinstance(creds, dict):
        result["provider_credentials"] = {
            k: ("***" if k in _SENSITIVE_CRED_KEYS else v) for k, v in creds.items()
        }
    for field in _INTERNAL_FIELDS:
        result.pop(field, None)
    return result


@router.post("")
async def create_phone_number(
    request: PhoneNumberCreate,
    org_ctx: dict = Depends(verify_global_key_with_org),
):
    try:
        data = request.model_dump(exclude_none=True)
        data["org_id"] = org_ctx["org_id"]
        phone = phone_number_service.create(**data)
        return _sanitize_phone_response(phone)
    except Exception as e:
        logger.error(f"Error creating phone number: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
async def list_phone_numbers(org_ctx: dict = Depends(verify_global_key_with_org)):
    try:
        phones = phone_number_service.get_active(org_id=org_ctx["org_id"])
        return {
            "phone_numbers": [_sanitize_phone_response(p) for p in phones],
            "total": len(phones),
        }
    except Exception as e:
        logger.error(f"Error listing phone numbers: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{phone_number_id}")
async def get_phone_number(
    phone_number_id: str,
    org_ctx: dict = Depends(verify_global_key_with_org),
):
    try:
        phone = phone_number_service.get_by_id(
            phone_number_id, org_id=org_ctx["org_id"]
        )
        if not phone:
            raise HTTPException(status_code=404, detail="Phone number not found")
        return _sanitize_phone_response(phone)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting phone number: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{phone_number_id}")
async def update_phone_number(
    phone_number_id: str,
    request: PhoneNumberUpdate,
    org_ctx: dict = Depends(verify_global_key_with_org),
):
    try:
        updates = request.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No updates provided")
        phone = phone_number_service.update(
            phone_number_id, org_id=org_ctx["org_id"], **updates
        )
        if not phone:
            raise HTTPException(status_code=404, detail="Phone number not found")
        return _sanitize_phone_response(phone)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating phone number: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{phone_number_id}")
async def delete_phone_number(
    phone_number_id: str,
    org_ctx: dict = Depends(verify_global_key_with_org),
):
    try:
        deleted = phone_number_service.delete(phone_number_id, org_id=org_ctx["org_id"])
        if not deleted:
            raise HTTPException(status_code=404, detail="Phone number not found")
        return {"status": "deleted", "id": phone_number_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting phone number: {e}")
        raise HTTPException(status_code=500, detail=str(e))
