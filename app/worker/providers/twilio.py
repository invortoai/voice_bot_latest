import asyncio
import json
from typing import Optional, Tuple

from fastapi import WebSocket
from loguru import logger
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from app.worker.assistant_service import get_inbound_call_config
from app.worker.config import SYSTEM_PARAM_KEYS
from app.worker.providers.base import WorkerProvider


async def _parse_twilio_websocket(websocket: WebSocket) -> Tuple[str, str, dict]:
    """Parse Twilio WebSocket start events to extract stream_sid, call_sid, custom_params."""
    stream_sid = None
    call_sid = None
    custom_params = {}

    for _ in range(2):
        try:
            message = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            data = json.loads(message)
            event = data.get("event")
            logger.debug(f"[twilio] Received event: {event}")

            if event == "start":
                start_data = data.get("start", {})
                stream_sid = start_data.get("streamSid", "")
                call_sid = start_data.get("callSid", "")
                custom_params = start_data.get("customParameters", {})
                logger.info(
                    f"[twilio] Stream started: stream_sid={stream_sid}, call_sid={call_sid}"
                )
                logger.debug(f"[twilio] Custom params: {custom_params}")

        except asyncio.TimeoutError:
            logger.error("[twilio] Timeout waiting for start message")
            raise
        except Exception as e:
            logger.error(f"[twilio] Error parsing message: {e}")
            raise

    if not stream_sid:
        raise ValueError("Failed to get stream_sid from Twilio start message")

    return stream_sid, call_sid, custom_params


class TwilioProvider(WorkerProvider):
    @property
    def name(self) -> str:
        return "twilio"

    async def parse_initial_message(
        self, websocket: WebSocket, path_call_sid: Optional[str] = None
    ) -> dict:
        stream_sid, call_sid, custom_params = await _parse_twilio_websocket(websocket)
        return {
            "stream_sid": stream_sid,
            "call_sid": call_sid,
            "custom_params": custom_params,
        }

    def extract_call_sid(
        self, call_info: dict, path_call_sid: Optional[str] = None
    ) -> str:
        return call_info.get("call_sid", "")

    def build_custom_params(self, call_sid: str, call_info: dict) -> dict:
        cp = call_info.get("custom_params", {})
        result = {
            "call_sid": call_sid,
            "call_type": cp.get("call_type", "inbound"),
            "caller": cp.get("caller", ""),
            "called": cp.get("called", ""),
            "to_number": cp.get("to_number", ""),
        }
        for k, v in cp.items():
            if k not in SYSTEM_PARAM_KEYS:
                result[k] = v
        return result

    async def config_fallback(
        self, call_sid: str, call_info: dict
    ) -> Tuple[Optional[dict], Optional[dict]]:
        cp = call_info.get("custom_params", {})
        called_number = cp.get("called", "")
        if not called_number:
            logger.error(
                f"[twilio] call_sid={call_sid}: no called number for fallback lookup"
            )
            return None, None

        logger.info(
            f"[twilio] call_sid={call_sid}: fallback phone lookup for {called_number}"
        )
        phone_config, assistant_config = await asyncio.to_thread(
            get_inbound_call_config, called_number
        )
        return phone_config, assistant_config

    def get_audio_params(self, call_info: dict) -> Tuple[int, int, str]:
        return 8000, 8000, "mulaw"

    def create_transport(
        self,
        websocket: WebSocket,
        vad_analyzer,
        call_info: dict,
        config,
        in_sample_rate: int,
        out_sample_rate: int,
    ):
        serializer = TwilioFrameSerializer(
            stream_sid=call_info["stream_sid"],
            call_sid=config.call_sid,
            account_sid=config.twilio_account_sid or None,
            auth_token=config.twilio_auth_token or None,
        )
        return FastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                add_wav_header=False,
                vad_enabled=True,
                vad_analyzer=vad_analyzer,
                vad_audio_passthrough=True,
                serializer=serializer,
            ),
        )
