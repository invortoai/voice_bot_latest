from typing import Optional

from fastapi import HTTPException
from loguru import logger
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

from app.config import PUBLIC_URL
from app.services.outbound.base import OutboundCallResult, OutboundProvider


class TwilioOutboundProvider(OutboundProvider):
    """Outbound call provider for Twilio."""

    def validate_credentials(self, phone_config: dict) -> None:
        creds = phone_config.get("provider_credentials") or {}
        if not creds.get("account_sid") or not creds.get("auth_token"):
            raise HTTPException(
                status_code=400,
                detail="Twilio credentials not configured for this phone number",
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
        account_sid = creds["account_sid"]
        auth_token = creds["auth_token"]
        from_number = phone_config["phone_number"]

        ws_url = worker.get_ws_url()
        twiml = _build_twiml(
            ws_url=ws_url,
            call_id=call_id,
            to_number=to_number,
            max_duration=phone_config.get("max_call_duration_seconds", 3600),
        )

        try:
            client = Client(account_sid, auth_token)
            call_kwargs = {"to": to_number, "from_": from_number, "twiml": twiml}
            if PUBLIC_URL:
                call_kwargs["status_callback"] = f"{PUBLIC_URL}/twilio/status"
                call_kwargs["status_callback_event"] = [
                    "initiated",
                    "ringing",
                    "answered",
                    "completed",
                ]
                call_kwargs["record"] = True
                call_kwargs["recording_channels"] = "dual"
                call_kwargs["recording_status_callback"] = (
                    f"{PUBLIC_URL}/twilio/recording-status"
                )
                call_kwargs["recording_status_callback_event"] = ["completed"]
            logger.info(
                f"Initiating Twilio outbound call: from {from_number} to {to_number}"
            )
            call = client.calls.create(**call_kwargs)
            logger.info(f"Twilio outbound call initiated: {call.sid}")
            return OutboundCallResult(call_sid=call.sid, from_number=from_number)
        except Exception as e:
            logger.error(f"Failed to initiate Twilio outbound call: {e}")
            raise HTTPException(status_code=500, detail=str(e))


def _build_twiml(
    ws_url: str,
    call_id: str,
    to_number: str,
    max_duration: int,
) -> str:
    """Build TwiML for a Twilio outbound call using the SDK for safe XML escaping."""
    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=ws_url)
    stream.parameter(name="call_type", value="outbound")
    stream.parameter(name="call_id", value=call_id)
    stream.parameter(name="to_number", value=to_number)
    connect.append(stream)
    response.append(connect)
    response.pause(length=max_duration)
    return str(response)
