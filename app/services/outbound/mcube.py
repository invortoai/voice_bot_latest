from typing import Optional

import httpx
from loguru import logger

from app.config import MCUBE_API_URL, MCUBE_AUTH_TOKEN, PUBLIC_URL
from app.services.outbound.base import OutboundCallResult, OutboundProvider
from app.utils.exceptions import raise_api_error


class McubeOutboundProvider(OutboundProvider):
    """Outbound call provider for MCube."""

    def validate_credentials(self, phone_config: dict) -> None:
        creds = phone_config.get("provider_credentials") or {}
        token = creds.get("token") or MCUBE_AUTH_TOKEN
        if not token:
            raise_api_error(
                400,
                "MCube authentication token is not configured. "
                "Please configure token in provider_credentials or MCUBE_AUTH_TOKEN environment variable.",
            )
        if not phone_config.get("phone_number"):
            raise_api_error(
                400,
                "Phone number is not configured for this phone number configuration.",
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
        creds = phone_config.get("provider_credentials") or {}
        token = creds.get("token") or MCUBE_AUTH_TOKEN
        from_number = phone_config["phone_number"]

        # MCube requires 10-digit numbers (no country code / + prefix).
        # Canonical E.164 is preserved in from_number for DB storage.
        payload = {
            "HTTP_AUTHORIZATION": token,
            "exenumber": _to_mcube_number(from_number),
            "custnumber": _to_mcube_number(to_number),
            "refurl": f"{PUBLIC_URL}/mcube/call",
            "refid": call_id,
        }

        logger.info(
            f"Initiating MCube outbound call: from {from_number} to {to_number}"
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{MCUBE_API_URL}/outbound-calls-websocket",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
        except httpx.TimeoutException:
            logger.error("MCube API request timed out")
            raise_api_error(
                504,
                "Request to MCube API timed out after 30 seconds.",
                timeout_seconds=30,
                call_id=call_id,
            )
        except httpx.RequestError as e:
            logger.error(f"Failed to connect to MCube API: {e}")
            raise_api_error(
                502, f"Failed to connect to MCube API: {str(e)}", call_id=call_id
            )

        if response.status_code not in [200, 201]:
            error_detail = response.text
            logger.error(
                f"MCube API HTTP error: status={response.status_code}, error={error_detail}"
            )
            raise_api_error(
                503,
                f"Failed to initiate call with MCube provider. "
                f"Provider returned status {response.status_code}.",
                provider_status=response.status_code,
                provider_error=error_detail[:200],
                call_id=call_id,
            )

        try:
            response_data = response.json()
        except ValueError:
            logger.error(f"MCube API response is not valid JSON: {response.text[:200]}")
            raise_api_error(
                502,
                "MCube API returned invalid JSON response.",
                response_text=response.text[:200],
                call_id=call_id,
            )

        if isinstance(response_data, dict):
            status_value = response_data.get("status")
            if status_value is False or status_value == "false":
                error_msg = response_data.get("msg", "Unknown error")
                logger.error(f"MCube API business error: status=false, msg={error_msg}")
                raise_api_error(
                    400,
                    f"MCube API rejected the request: {error_msg}",
                    mcube_status=status_value,
                    mcube_message=error_msg,
                    call_id=call_id,
                )

        call_sid = response_data.get("callid")
        if not call_sid:
            logger.error(f"MCube API response missing 'callid' field: {response_data}")
            raise_api_error(
                502, "MCube API response missing 'callid' field.", call_id=call_id
            )

        logger.info(f"MCube outbound call initiated: {call_sid}")
        return OutboundCallResult(call_sid=call_sid, from_number=from_number)


def _to_mcube_number(number: str) -> str:
    """Normalize a phone number to the 10-digit format MCube expects.

    MCube rejects E.164 format (+91XXXXXXXXXX). We strip everything except
    the last 10 digits, which matches the convention used throughout the
    MCube webhook handler (e.g. caller[-10:]).
    """
    digits = "".join(ch for ch in number if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits
