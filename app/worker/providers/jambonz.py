import asyncio
import json
import uuid
from typing import Optional, Tuple

from fastapi import WebSocket
from loguru import logger

from app.worker.assistant_service import (
    get_assistant_by_id,
    get_inbound_call_config,
    get_phone_number_config,
)
from app.worker.config import SYSTEM_PARAM_KEYS
from app.worker.providers.base import WorkerProvider


class JambonzProvider(WorkerProvider):
    @property
    def name(self) -> str:
        return "jambonz"

    async def parse_initial_message(
        self, websocket: WebSocket, path_call_sid: Optional[str] = None
    ) -> dict:
        call_info = {}
        try:
            message_data = await asyncio.wait_for(websocket.receive(), timeout=10.0)
            logger.info(f"[jambonz] Initial message type: {list(message_data.keys())}")

            if "text" in message_data:
                text = message_data["text"]
                logger.debug(f"[jambonz] Message length: {len(text)} bytes")
                try:
                    call_info = json.loads(text)
                    logger.info(f"[jambonz] Parsed JSON keys: {list(call_info.keys())}")
                except json.JSONDecodeError as e:
                    logger.warning(
                        f"[jambonz] Full JSON parse failed: {e}, trying raw_decode"
                    )
                    try:
                        decoder = json.JSONDecoder()
                        call_info, idx = decoder.raw_decode(text)
                        logger.info(
                            f"[jambonz] Extracted first JSON object (ended at char {idx})"
                        )
                    except Exception as e2:
                        logger.error(f"[jambonz] Failed to extract JSON: {e2}")
                        call_info = {}
            elif "bytes" in message_data:
                logger.warning("[jambonz] First message is binary — no JSON metadata")
                call_info = {"first_audio_frame": message_data["bytes"]}
            else:
                logger.warning(
                    f"[jambonz] Unexpected message type: {message_data.keys()}"
                )

        except asyncio.TimeoutError:
            logger.error("[jambonz] Timeout waiting for initial message")
        except Exception as e:
            logger.error(
                f"[jambonz] Error receiving initial message: {e}", exc_info=True
            )

        return call_info

    def extract_call_sid(
        self, call_info: dict, path_call_sid: Optional[str] = None
    ) -> str:
        return (
            call_info.get("callSid")
            or call_info.get("call_sid")
            or call_info.get("callId")
            or call_info.get("call_id")
            or str(uuid.uuid4())
        )

    def build_custom_params(self, call_sid: str, call_info: dict) -> dict:
        custom = call_info.get("customParameters") or call_info.get("metadata") or {}
        to_number = (
            call_info.get("to")
            or (custom.get("called") if isinstance(custom, dict) else "")
            or ""
        )
        from_number = (
            call_info.get("from")
            or (custom.get("caller") if isinstance(custom, dict) else "")
            or ""
        )
        call_type = (
            custom.get("call_type") if isinstance(custom, dict) else None
        ) or "inbound"
        result = {
            "call_sid": call_sid,
            "call_type": call_type,
            "caller": from_number,
            "called": to_number,
            "to_number": to_number,
        }
        # Extract nested user custom_params if present in the metadata blob
        nested_custom = (
            custom.get("custom_params") if isinstance(custom, dict) else None
        )
        if isinstance(nested_custom, dict):
            for k, v in nested_custom.items():
                if k not in SYSTEM_PARAM_KEYS:
                    result[k] = v
        return result

    async def config_fallback(
        self, call_sid: str, call_info: dict
    ) -> Tuple[Optional[dict], Optional[dict]]:
        custom = call_info.get("customParameters") or call_info.get("metadata") or {}
        if not isinstance(custom, dict):
            custom = {}

        assistant_id = custom.get("assistant_id")
        if assistant_id:
            logger.info(
                f"[jambonz] call_sid={call_sid}: fetching assistant by id={assistant_id}"
            )
            assistant_config = await asyncio.to_thread(
                get_assistant_by_id, str(assistant_id)
            )
            if not assistant_config:
                logger.error(
                    f"[jambonz] call_sid={call_sid}: assistant not found: {assistant_id}"
                )
                return None, None

            phone_config = None
            to_number = call_info.get("to") or custom.get("called") or ""
            call_type = custom.get("call_type", "inbound")
            if call_type == "inbound" and to_number:
                phone_config = await asyncio.to_thread(
                    get_phone_number_config, to_number
                )
            return phone_config, assistant_config

        # Last resort: phone number lookup
        to_number = call_info.get("to") or custom.get("called") or ""
        from_number = call_info.get("from") or custom.get("caller") or ""
        call_type = custom.get("call_type", "inbound")
        lookup = to_number if call_type == "inbound" else from_number

        if not lookup:
            logger.error(
                f"[jambonz] call_sid={call_sid}: no number available for fallback lookup"
            )
            return None, None

        logger.info(
            f"[jambonz] call_sid={call_sid}: fallback phone lookup for {lookup}"
        )
        phone_config, assistant_config = await asyncio.to_thread(
            get_inbound_call_config, lookup
        )
        return phone_config, assistant_config

    def get_audio_params(self, call_info: dict) -> Tuple[int, int, str]:
        in_rate = int(call_info.get("sampleRate") or 8000)
        nested = call_info.get("metadata") or call_info.get("customParameters") or {}

        def _get(key, default=None):
            if isinstance(nested, dict) and key in nested:
                return nested[key]
            return call_info.get(key, default)

        out_rate = int(_get("jambonz_audio_out_sample_rate", in_rate) or in_rate)
        return in_rate, out_rate, "linear16"

    def create_transport(
        self,
        websocket: WebSocket,
        vad_analyzer,
        call_info: dict,
        config,
        in_sample_rate: int,
        out_sample_rate: int,
    ):
        from app.worker.jambonz import (
            JambonzFrameSerializer,
            JambonzTransport,
            JambonzTransportParams,
        )

        nested = call_info.get("metadata") or call_info.get("customParameters") or {}

        def _get(key, default=None):
            if isinstance(nested, dict) and key in nested:
                return nested[key]
            return call_info.get(key, default)

        audio_out_streaming = bool(_get("jambonz_bidirectional_streaming", False))

        logger.info(
            f"[jambonz] Audio config: in_rate={in_sample_rate}, out_rate={out_sample_rate}, "
            f"streaming={audio_out_streaming}"
        )

        serializer = JambonzFrameSerializer(
            JambonzFrameSerializer.InputParams(
                audio_in_sample_rate=in_sample_rate,
                audio_out_sample_rate=out_sample_rate,
                stt_sample_rate=in_sample_rate,
                audio_in_encoding="linear16",
                audio_out_encoding="linear16",
                audio_out_streaming=audio_out_streaming,
                auto_hang_up=True,
            )
        )
        return JambonzTransport(
            websocket=websocket,
            params=JambonzTransportParams(
                serializer=serializer,
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_out_10ms_chunks=2,
                vad_analyzer=vad_analyzer,
            ),
        )
