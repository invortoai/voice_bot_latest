import asyncio
import json
import uuid
from typing import Optional, Tuple

from fastapi import WebSocket
from loguru import logger

from app.worker.providers.base import WorkerProvider


class McubeProvider(WorkerProvider):
    @property
    def name(self) -> str:
        return "mcube"

    async def parse_initial_message(
        self, websocket: WebSocket, path_call_sid: Optional[str] = None
    ) -> dict:
        call_info = {}
        try:
            message_data = await asyncio.wait_for(websocket.receive(), timeout=10.0)
            logger.info(
                f"[mcube] call_sid={path_call_sid}: initial message type: {list(message_data.keys())}"
            )

            if "text" in message_data:
                text = message_data["text"]
                logger.info(
                    f"[mcube] call_sid={path_call_sid}: text message length: {len(text)} bytes"
                )
                try:
                    call_info = json.loads(text)
                    logger.info(
                        f"[mcube] call_sid={path_call_sid}: parsed JSON keys: {list(call_info.keys())}"
                    )
                except json.JSONDecodeError as e:
                    logger.warning(
                        f"[mcube] call_sid={path_call_sid}: JSON parse failed: {e}, trying raw_decode"
                    )
                    try:
                        decoder = json.JSONDecoder()
                        call_info, _ = decoder.raw_decode(text)
                    except Exception as e2:
                        logger.error(
                            f"[mcube] call_sid={path_call_sid}: failed to extract JSON: {e2}"
                        )
                        call_info = {}
            else:
                logger.warning(
                    f"[mcube] call_sid={path_call_sid}: unexpected message type: {message_data.keys()}"
                )

        except asyncio.TimeoutError:
            logger.error(
                f"[mcube] call_sid={path_call_sid}: timeout waiting for initial message"
            )
        except Exception as e:
            logger.error(
                f"[mcube] call_sid={path_call_sid}: error receiving initial message: {e}",
                exc_info=True,
            )

        # Validate the start message structure early so errors are caught before config lookup
        from app.models.mcube_messages import McubeStartMessage

        try:
            initial_msg = McubeStartMessage.from_dict(call_info)
            logger.info(
                f"[mcube] call_sid={path_call_sid}: start event validated — "
                f"callId={initial_msg.start.call_id}, streamId={initial_msg.start.stream_id}"
            )
        except ValueError as e:
            raise ValueError(f"Invalid MCube start message: {e}") from e
        except Exception as e:
            raise ValueError(f"Failed to parse MCube start message: {e}") from e

        return call_info

    def extract_call_sid(
        self, call_info: dict, path_call_sid: Optional[str] = None
    ) -> str:
        return path_call_sid or ""

    def build_custom_params(self, call_sid: str, call_info: dict) -> dict:
        # MCube always requires a pre-existing call record; this is a safety fallback only
        return {
            "call_sid": call_sid,
            "call_type": "inbound",
            "caller": "",
            "called": "",
            "to_number": "",
        }

    async def config_fallback(
        self, call_sid: str, call_info: dict
    ) -> Tuple[Optional[dict], Optional[dict]]:
        # MCube always requires a call record — no phone-based fallback
        logger.error(
            f"[mcube] call_sid={call_sid}: no call record found — cannot proceed without call record"
        )
        return None, None

    def get_audio_params(self, call_info: dict) -> Tuple[int, int, str]:
        from app.models.mcube_messages import McubeStartMessage

        try:
            msg = McubeStartMessage.from_dict(call_info)
            if msg.start.media_format:
                encoding = msg.start.media_format.encoding_type
                rate = msg.start.media_format.sample_rate
            else:
                encoding = "mulaw"
                rate = 8000
        except Exception:
            encoding = "mulaw"
            rate = 8000

        return rate, rate, encoding

    def create_transport(
        self,
        websocket: WebSocket,
        vad_analyzer,
        call_info: dict,
        config,
        in_sample_rate: int,
        out_sample_rate: int,
    ):
        from app.models.mcube_messages import McubeStartMessage
        from app.worker.mcube.serializer import McubeFrameSerializer
        from app.worker.mcube.transport import McubeTransport, McubeTransportParams

        try:
            msg = McubeStartMessage.from_dict(call_info)
            stream_id = msg.start.stream_id or str(uuid.uuid4())
        except Exception:
            stream_id = str(uuid.uuid4())

        _, _, encoding = self.get_audio_params(call_info)

        logger.info(
            f"[mcube] call_sid={config.call_sid}: audio config: encoding={encoding}, "
            f"rate={in_sample_rate}, stream_id={stream_id}"
        )

        serializer = McubeFrameSerializer(
            McubeFrameSerializer.InputParams(
                audio_in_sample_rate=in_sample_rate,
                audio_out_sample_rate=out_sample_rate,
                stt_sample_rate=in_sample_rate,
                audio_in_encoding=encoding,
                audio_out_encoding=encoding,
                stream_id=stream_id,
                auto_hang_up=True,
            )
        )
        return McubeTransport(
            websocket=websocket,
            params=McubeTransportParams(
                serializer=serializer,
                audio_in_enabled=True,
                audio_out_enabled=True,
                add_wav_header=False,
                vad_enabled=True,
                vad_analyzer=vad_analyzer,
                vad_audio_passthrough=True,
                audio_in_sample_rate=in_sample_rate,
                audio_out_sample_rate=out_sample_rate,
            ),
        )
