from decimal import Decimal
from typing import Optional

import httpx
from fastapi import HTTPException
from loguru import logger

from app.config import (
    JAMBONZ_ACCOUNT_SID,
    JAMBONZ_API_KEY,
    JAMBONZ_API_URL,
    JAMBONZ_APPLICATION_SID,
    JAMBONZ_WEBHOOK_SECRET,
    PUBLIC_URL,
)
from app.services.outbound.base import OutboundCallResult, OutboundProvider


class JambonzOutboundProvider(OutboundProvider):
    """Outbound call provider for Jambonz."""

    def validate_credentials(self, phone_config: dict) -> None:
        if (
            not JAMBONZ_ACCOUNT_SID
            or not JAMBONZ_API_KEY
            or not JAMBONZ_APPLICATION_SID
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Jambonz credentials (JAMBONZ_ACCOUNT_SID, JAMBONZ_API_KEY, "
                    "JAMBONZ_APPLICATION_SID) not configured in environment"
                ),
            )
        trunk_name = (phone_config.get("provider_credentials") or {}).get("trunk_name")
        if not trunk_name:
            logger.warning(
                "Jambonz trunk name not configured for this phone number. "
                "Jambonz will use default routing."
            )

    async def initiate(
        self,
        call_id: str,
        phone_config: dict,
        assistant_config: dict,
        worker,
        to_number: str,
        custom_params: Optional[dict],
    ) -> OutboundCallResult:
        from_number = phone_config.get("phone_number")
        trunk_name = (phone_config.get("provider_credentials") or {}).get("trunk_name")

        call_metadata = {
            "call_id": call_id,
            "call_type": "outbound",
            "to_number": to_number,
            "assistant_id": str(assistant_config.get("id", "")),
            "phone_number_id": str(phone_config.get("id", "")),
        }

        to_object = {"type": "phone", "number": to_number}
        if trunk_name:
            to_object["trunk"] = trunk_name

        jambonz_payload = {
            "application_sid": JAMBONZ_APPLICATION_SID,
            "from": from_number,
            "to": to_object,
            "tag": _convert_decimals(call_metadata),
        }
        if PUBLIC_URL:
            status_hook = {"url": f"{PUBLIC_URL}/jambonz/status", "method": "POST"}
            call_hook = {"url": f"{PUBLIC_URL}/jambonz/call", "method": "POST"}
            if JAMBONZ_WEBHOOK_SECRET:
                status_hook["username"] = "invorto"
                status_hook["password"] = JAMBONZ_WEBHOOK_SECRET
                call_hook["username"] = "invorto"
                call_hook["password"] = JAMBONZ_WEBHOOK_SECRET
            jambonz_payload["call_status_hook"] = status_hook
            jambonz_payload["call_hook"] = call_hook

        api_endpoint = f"{JAMBONZ_API_URL}/v1/Accounts/{JAMBONZ_ACCOUNT_SID}/Calls"
        logger.info(
            f"Initiating Jambonz outbound call: from {from_number} to {to_number}"
        )

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    api_endpoint,
                    json=jambonz_payload,
                    headers={
                        "Authorization": f"Bearer {JAMBONZ_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    timeout=30.0,
                )
        except httpx.TimeoutException:
            logger.error("Jambonz API request timed out")
            raise HTTPException(status_code=504, detail="Jambonz API request timed out")
        except httpx.RequestError as e:
            logger.error(f"Failed to connect to Jambonz API: {e}")
            raise HTTPException(
                status_code=502, detail=f"Failed to connect to Jambonz API: {str(e)}"
            )

        if response.status_code != 201:
            error_detail = response.text
            try:
                error_json = response.json()
                error_msg = (
                    error_json.get("msg")
                    or error_json.get("message")
                    or error_json.get("error")
                    or error_detail
                )
            except Exception:
                error_msg = error_detail
            logger.error(f"Jambonz API error: {response.status_code} - {error_detail}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Jambonz API error: {error_msg}",
            )

        result = response.json()
        call_sid = result.get("sid") or result.get("call_sid") or call_id
        logger.info(f"Jambonz outbound call initiated: {call_sid}")
        return OutboundCallResult(call_sid=call_sid, from_number=from_number)


def _convert_decimals(obj):
    """Recursively convert Decimal values to float for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_decimals(i) for i in obj]
    elif isinstance(obj, Decimal):
        return float(obj)
    return obj
