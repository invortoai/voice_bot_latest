import asyncio
import json
import time
from typing import Optional

from loguru import logger
from deepgram import LiveOptions
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import (
    ElevenLabsTTSService,
    ELEVENLABS_MULTILINGUAL_MODELS,
    output_format_from_sample_rate,
)

try:
    from websockets.asyncio.client import connect as websocket_connect
    from websockets.protocol import State
except ModuleNotFoundError:
    pass

from app.config import ELEVENLABS_API_KEY, DEEPGRAM_API_KEY
from app.worker.config import AssistantConfig


# Default inactivity timeout for ElevenLabs WebSocket (max 180 seconds)
ELEVENLABS_INACTIVITY_TIMEOUT = 180

# Keep-alive interval (send space character every 10 seconds per ElevenLabs docs)
KEEPALIVE_INTERVAL = 10


class PrewarmableDeepgramSTTService(DeepgramSTTService):
    """
    Extended Deepgram STT service with pre-warming support.

    Pre-warms the WebSocket connection BEFORE the pipeline starts, reducing
    the ~2 second delay during StartFrame propagation.
    """

    def __init__(self, *, sample_rate: int = 8000, **kwargs):
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._prewarmed = False
        # Pre-set sample_rate in settings so we can connect before start()
        self._settings["sample_rate"] = sample_rate

    async def prewarm(self) -> bool:
        """
        Pre-warm the STT connection by establishing the WebSocket BEFORE the pipeline starts.

        Returns:
            True if pre-warming was successful, False otherwise
        """
        start_time = time.time()

        try:
            logger.info("Pre-warming Deepgram STT connection...")

            # Connect to Deepgram
            await self._connect()

            # Check if connection was actually established (not just object created)
            if (
                hasattr(self, "_connection")
                and self._connection
                and await self._connection.is_connected()
            ):
                self._prewarmed = True
                elapsed_ms = (time.time() - start_time) * 1000
                logger.info(
                    f"Deepgram STT pre-warmed successfully in {elapsed_ms:.0f}ms"
                )
                return True
            else:
                logger.warning(
                    "Deepgram STT pre-warm failed: connection not established"
                )
                return False

        except Exception as e:
            logger.error(f"Deepgram STT pre-warm error: {e}")
            self._prewarmed = False
            return False

    async def start(self, frame):
        """
        Override start to skip reconnecting if already pre-warmed.
        """
        # Call parent's parent start (skip DeepgramSTTService's start which calls _connect)
        from pipecat.services.stt_service import STTService

        await STTService.start(self, frame)

        # Update sample_rate from frame if different (unlikely but possible)
        self._settings["sample_rate"] = self.sample_rate

        if self._prewarmed and hasattr(self, "_connection") and self._connection:
            logger.info("Deepgram STT starting with pre-warmed connection")
        else:
            # Not pre-warmed, connect now
            logger.debug("Deepgram STT not pre-warmed, connecting now...")
            await self._connect()

    async def close_prewarm(self) -> None:
        """Close pre-warmed connection without using it (call abandoned / params mismatch)."""
        if not self._prewarmed:
            return
        try:
            if hasattr(self, "_connection") and self._connection:
                try:
                    await self._connection.finish()
                except Exception:
                    pass
            self._prewarmed = False
        except Exception as e:
            logger.debug(f"Deepgram STT close_prewarm error: {e}")

    @property
    def is_prewarmed(self) -> bool:
        """Check if the STT connection has been pre-warmed."""
        return (
            self._prewarmed
            and hasattr(self, "_connection")
            and self._connection is not None
        )


class PrewarmableElevenLabsTTSService(ElevenLabsTTSService):
    """
    Extended ElevenLabs TTS service with pre-warming and extended inactivity timeout.

    Features:
    - inactivity_timeout parameter to keep WebSocket open longer (up to 180 seconds)
    - Pre-warming support to establish WebSocket BEFORE the pipeline starts
    - Keep-alive mechanism to maintain connection during pre-warming phase
    - Seamless handoff to Pipecat's internal keep-alive when pipeline starts
    """

    def __init__(
        self,
        *,
        inactivity_timeout: int = ELEVENLABS_INACTIVITY_TIMEOUT,
        sample_rate: int = 24000,
        **kwargs,
    ):
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._inactivity_timeout = min(inactivity_timeout, 180)  # Max 180 seconds
        self._prewarmed = False
        self._prewarm_keepalive_task: Optional[asyncio.Task] = None

        # Pre-compute output_format so we can connect before pipeline starts
        # This is normally set in start(), but we need it earlier for pre-warming
        self._output_format = output_format_from_sample_rate(sample_rate)

    async def prewarm(self) -> bool:
        """
        Pre-warm the TTS connection by establishing the WebSocket BEFORE the pipeline starts.

        This should be called after creating the service but before the telephony
        WebSocket connection is established, so TTS is ready when audio starts flowing.

        Returns:
            True if pre-warming was successful, False otherwise
        """
        start_time = time.time()

        try:
            logger.info("Pre-warming ElevenLabs TTS connection...")

            # Establish WebSocket connection
            await self._connect_websocket()

            if self._websocket and self._websocket.state is State.OPEN:
                # Start our own keep-alive task (will be superseded by Pipecat's when pipeline starts)
                self._prewarm_keepalive_task = asyncio.create_task(
                    self._prewarm_keepalive_handler()
                )

                elapsed_ms = (time.time() - start_time) * 1000
                logger.info(
                    f"ElevenLabs TTS pre-warmed successfully in {elapsed_ms:.0f}ms"
                )
                return True
            else:
                logger.warning(
                    "ElevenLabs TTS pre-warm failed: WebSocket not connected"
                )
                return False

        except Exception as e:
            logger.error(f"ElevenLabs TTS pre-warm error: {e}")
            self._prewarmed = False
            return False

    async def _prewarm_keepalive_handler(self):
        """
        Keep-alive handler for the pre-warming phase.

        Sends a space character every KEEPALIVE_INTERVAL seconds to keep the
        WebSocket connection open before the pipeline starts.

        This task is automatically cancelled when the pipeline's internal
        keep-alive takes over.
        """
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                try:
                    if self._websocket and self._websocket.state is State.OPEN:
                        # Send space character as keep-alive (per ElevenLabs docs)
                        # We use the context-aware format if context exists, otherwise simple format
                        if hasattr(self, "_context_id") and self._context_id:
                            keepalive_msg = json.dumps(
                                {
                                    "text": " ",
                                    "context_id": self._context_id,
                                }
                            )
                        else:
                            # During pre-warm, no context exists yet, send minimal keep-alive
                            keepalive_msg = json.dumps({"text": " "})

                        await self._websocket.send(keepalive_msg)
                        logger.trace("Sent ElevenLabs pre-warm keep-alive")
                    else:
                        logger.debug(
                            "ElevenLabs pre-warm: WebSocket closed, stopping keep-alive"
                        )
                        break
                except Exception as e:
                    logger.warning(f"ElevenLabs pre-warm keep-alive error: {e}")
                    break
        except asyncio.CancelledError:
            logger.debug("ElevenLabs pre-warm keep-alive task cancelled")

    async def start(self, frame):
        """
        Override start to handle transition from pre-warmed state.

        If already pre-warmed, we skip the initial connection but let the
        parent class set up its internal tasks.
        """
        # Cancel our pre-warm keep-alive task - Pipecat's will take over
        if self._prewarm_keepalive_task:
            self._prewarm_keepalive_task.cancel()
            try:
                await self._prewarm_keepalive_task
            except asyncio.CancelledError:
                pass
            self._prewarm_keepalive_task = None

        # The parent's start() will:
        # 1. Set _output_format (we've already done this, but it's idempotent)
        # 2. Call _connect() which calls _connect_websocket() (has guard for already-open socket)
        # 3. Start receive and keepalive tasks
        await super().start(frame)

        if self._prewarmed:
            logger.info("ElevenLabs TTS starting with pre-warmed connection")

    async def _connect_websocket(self):
        """
        Override to add inactivity_timeout parameter to WebSocket URL.
        """
        try:
            if self._websocket and self._websocket.state is State.OPEN:
                logger.debug("ElevenLabs WebSocket already connected (pre-warmed)")
                return

            logger.debug(
                f"Connecting to ElevenLabs (inactivity_timeout={self._inactivity_timeout}s)"
            )

            voice_id = self._voice_id
            model = self.model_name
            output_format = self._output_format

            # Build URL with inactivity_timeout parameter
            url = (
                f"{self._url}/v1/text-to-speech/{voice_id}/multi-stream-input"
                f"?model_id={model}"
                f"&output_format={output_format}"
                f"&auto_mode={self._settings['auto_mode']}"
                f"&inactivity_timeout={self._inactivity_timeout}"
            )

            if self._settings["enable_ssml_parsing"]:
                url += f"&enable_ssml_parsing={self._settings['enable_ssml_parsing']}"

            if self._settings["enable_logging"]:
                url += f"&enable_logging={self._settings['enable_logging']}"

            if self._settings["apply_text_normalization"] is not None:
                url += f"&apply_text_normalization={self._settings['apply_text_normalization']}"

            # Language can only be used with the ELEVENLABS_MULTILINGUAL_MODELS
            language = self._settings["language"]
            if model in ELEVENLABS_MULTILINGUAL_MODELS and language is not None:
                url += f"&language_code={language}"
                logger.debug(f"Using language code: {language}")
            elif language is not None:
                logger.warning(
                    f"Language code [{language}] not applied. Language codes can only be used with multilingual models: {', '.join(sorted(ELEVENLABS_MULTILINGUAL_MODELS))}"
                )

            # Set max websocket message size to 16MB for large audio responses
            self._websocket = await websocket_connect(
                url,
                max_size=16 * 1024 * 1024,
                additional_headers={"xi-api-key": self._api_key},
            )

            # Capture xi-request-id from the WebSocket handshake response headers.
            # Log it so call_sid ↔ xi-request-id can be cross-referenced in the
            # ElevenLabs console (ElevenLabs has no client-side tag mechanism).
            try:
                resp_headers = self._websocket.response.headers
                xi_trace_id = resp_headers.get("X-Trace-ID")
                if xi_trace_id:
                    logger.info(f"ElevenLabs X-Trace-ID: {xi_trace_id}")
            except Exception:
                logger.warning("Unable to get ElevenLabs X-Trace-ID")

            self._prewarmed = True
            await self._call_event_handler("on_connected")
            logger.info("ElevenLabs WebSocket connected successfully")

        except Exception as e:
            self._websocket = None
            self._prewarmed = False
            await self.push_error(
                error_msg=f"ElevenLabs connection error: {e}", exception=e
            )
            await self._call_event_handler("on_connection_error", f"{e}")

    async def close_prewarm(self) -> None:
        """Close pre-warmed connection without using it (call abandoned / params mismatch)."""
        if self._prewarm_keepalive_task:
            self._prewarm_keepalive_task.cancel()
            try:
                await self._prewarm_keepalive_task
            except asyncio.CancelledError:
                pass
            self._prewarm_keepalive_task = None
        if self._websocket:
            try:
                await self._websocket.close()
            except Exception:
                pass
            self._websocket = None
        self._prewarmed = False

    @property
    def is_prewarmed(self) -> bool:
        """Check if the TTS connection has been pre-warmed."""
        return self._prewarmed and self._websocket is not None


def create_stt_service(
    config: AssistantConfig,
    *,
    sample_rate: int = 8000,
    encoding: str = "linear16",
):
    """
    Create an STT service based on the assistant configuration.

    Uses PrewarmableDeepgramSTTService with pre-warming support
    to reduce latency during pipeline startup.

    Args:
        config: Assistant configuration with transcriber settings
        sample_rate: Audio sample rate (default: 8000 for telephony)
        encoding: Audio encoding (default: linear16)

    Returns:
        Configured STT service instance
    """
    # Build LiveOptions from config so Deepgram actually sees them.
    # model/language/encoding passed as **kwargs are silently forwarded to
    # STTService (the grandparent) and never reach Deepgram configuration.
    live_options_kwargs: dict = {
        "model": config.transcriber_model,
        "language": config.transcriber_language,
        "encoding": encoding,
    }
    # Merge additional Deepgram options stored in transcriber_settings JSONB
    # (e.g. punctuate, smart_format, filler_words, endpointing, diarize, etc.)
    live_options_kwargs.update(config.transcriber_settings)

    # Tag the Deepgram session with call_sid so it's searchable in the console.
    tags = [config.call_sid] if config.call_sid else []
    if tags:
        live_options_kwargs["tag"] = tags

    logger.info(
        f"Creating Deepgram STT: model={live_options_kwargs['model']!r}, "
        f"language={live_options_kwargs['language']!r}, "
        f"encoding={live_options_kwargs['encoding']!r}, "
        f"sample_rate={sample_rate}, "
        f"tag={live_options_kwargs.get('tag')}, "
        f"extra={config.transcriber_settings or '{}'}"
    )

    live_options = LiveOptions(**live_options_kwargs)

    return PrewarmableDeepgramSTTService(
        api_key=DEEPGRAM_API_KEY,
        sample_rate=sample_rate,
        live_options=live_options,
    )


def create_tts_service(config: AssistantConfig, *, sample_rate: int = 8000):
    """
    Create a TTS service based on the assistant configuration.

    Uses PrewarmableElevenLabsTTSService with extended inactivity timeout
    to reduce latency on first TTS request.

    Args:
        config: Assistant configuration with voice settings
        sample_rate: Audio sample rate (default: 8000 for telephony)

    Returns:
        Configured TTS service instance
    """
    provider = config.voice_provider.lower()

    if provider == "elevenlabs":
        # voice_settings kwargs passed directly are silently forwarded to the
        # parent TTSService base class and never reach ElevenLabs configuration.
        # They must be passed through params=InputParams(...) instead.
        params = (
            ElevenLabsTTSService.InputParams(**config.voice_settings)
            if config.voice_settings
            else None
        )

        logger.info(
            f"Creating ElevenLabs TTS: model={config.voice_model!r}, "
            f"voice_id={config.voice_id!r}, "
            f"sample_rate={sample_rate}, "
            f"params={config.voice_settings or '{}'}"
        )

        return PrewarmableElevenLabsTTSService(
            api_key=ELEVENLABS_API_KEY,
            model=config.voice_model,
            voice_id=config.voice_id,
            url="wss://api.in.residency.elevenlabs.io",
            sample_rate=sample_rate,
            params=params,
            inactivity_timeout=ELEVENLABS_INACTIVITY_TIMEOUT,
        )

    else:
        logger.warning(
            f"Unknown voice provider '{provider}', falling back to ElevenLabs"
        )
        return PrewarmableElevenLabsTTSService(
            api_key=ELEVENLABS_API_KEY,
            model=config.voice_model,
            voice_id=config.voice_id,
            url="wss://api.in.residency.elevenlabs.io",
            sample_rate=sample_rate,
            inactivity_timeout=ELEVENLABS_INACTIVITY_TIMEOUT,
        )
