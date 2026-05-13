import asyncio
import io
import ipaddress
import time
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import aiohttp.abc
from fastapi import WebSocket
from loguru import logger

from app.config import IS_LOCAL
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    LLMMessagesAppendFrame,
    OutputAudioRawFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.openai.base_llm import BaseOpenAILLMService
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContext,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from app.config import OPENAI_API_KEY, SILENCE_NUDGE_AI_PROMPT, SILENCE_NUDGE_STATIC_PROMPT_TEMPLATE
from app.worker.config import AssistantConfig, SYSTEM_PARAM_KEYS
from app.worker.strategies.llm_interruption_judge import LLMInterruptionJudgeStrategy
from app.worker.metrics import CallMetrics
from app.worker.pipecat_tracing import is_pipecat_tracing_enabled
from app.worker.processors.end_call import EndCallProcessor
from app.worker.processors.metrics import MetricsProcessor, TranscriptionStatsProcessor
from app.worker.providers.base import WorkerProvider
from app.worker.services import create_stt_service, create_tts_service

MAX_AUDIO_FETCH_BYTES = 10 * 1024 * 1024  # 10 MB


# =============================================================================
# Silence Nudge Observer
# =============================================================================


class SilenceNudgeObserver(BaseObserver):
    """Fires a silence nudge when the user is quiet past silence_timeout_seconds.

    Uses frame observation to track user and bot speech state, then runs an
    asyncio timer. Resets on every UserStartedSpeakingFrame. Pauses while the
    bot is speaking (BotStartedSpeakingFrame) and restarts when the bot
    finishes (BotStoppedSpeakingFrame). Works with pipecat 0.0.99+.
    """

    def __init__(self, config, call_sid: str, provider_name: str):
        super().__init__()
        self._config = config
        self._call_sid = call_sid
        self._provider_name = provider_name
        self._task_ref = None          # set after PipelineTask is created
        self._timer: Optional[asyncio.Task] = None
        self._bot_speaking = False
        self._active = False           # armed after task is set

    def set_pipeline_task(self, task) -> None:
        """Called after PipelineTask is created to wire up the queue_frames ref."""
        self._task_ref = task
        self._active = True
        # Start the initial timer immediately so the nudge fires even if the
        # user never produces any VAD events (pure silence from call start).
        self._start_timer()

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if isinstance(frame, UserStartedSpeakingFrame):
            # User is speaking — reset the silence timer.
            self._reset_timer()
        elif isinstance(frame, BotStartedSpeakingFrame):
            # Bot is speaking — pause the timer so we don't nudge over the bot.
            self._bot_speaking = True
            self._cancel_timer()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            # Bot finished — restart the silence window.
            self._bot_speaking = False
            self._start_timer()
        elif isinstance(frame, EndFrame):
            # Call ending — stop everything.
            self._active = False
            self._cancel_timer()

    def _cancel_timer(self) -> None:
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    def _reset_timer(self) -> None:
        self._cancel_timer()
        if not self._bot_speaking:
            self._start_timer()

    def _start_timer(self) -> None:
        self._cancel_timer()
        if self._active and self._task_ref is not None:
            self._timer = asyncio.create_task(self._run_timer())

    async def _run_timer(self) -> None:
        try:
            await asyncio.sleep(self._config.silence_timeout_seconds)
            if not self._active or self._bot_speaking or self._task_ref is None:
                return
            logger.info(
                f"[{self._provider_name}] call_sid={self._call_sid}: silence nudge "
                f"firing (type={self._config.silence_response_type})"
            )
            if self._config.silence_response_type == "ai_generated":
                content = SILENCE_NUDGE_AI_PROMPT
            else:
                msg = self._config.silence_response_message
                if not msg:
                    logger.warning(
                        f"[{self._provider_name}] call_sid={self._call_sid}: static silence "
                        f"nudge enabled but silence_response_message is empty — skipping"
                    )
                    return
                content = SILENCE_NUDGE_STATIC_PROMPT_TEMPLATE.format(message=msg)
            await self._task_ref.queue_frames(
                [LLMMessagesAppendFrame(messages=[{"role": "user", "content": content}], run_llm=True)]
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                f"[{self._provider_name}] call_sid={self._call_sid}: silence nudge error: {e}"
            )


# =============================================================================
# Audio Utilities
# =============================================================================


def is_audio_url(text: str) -> bool:
    if not text:
        return False

    text = text.strip()
    if not text.lower().startswith(("http://", "https://")):
        return False

    url_path = text.split("?")[0].lower()
    audio_extensions = (
        ".mp3",
        ".wav",
        ".ogg",
        ".m4a",
        ".aac",
        ".flac",
        ".webm",
        ".pcm",
        ".mp4",
    )
    is_audio = any(url_path.endswith(ext) for ext in audio_extensions)
    logger.debug(f"is_audio_url: '{text[:100]}' -> {is_audio}")
    return is_audio


def _validate_audio_url(url: str) -> list[tuple[int, str]]:
    """SSRF validation for audio fetch URLs.

    Blocks private IPs, localhost, link-local, and AWS metadata endpoint.
    Returns list of (socket_family, ip_str) for pinned DNS resolution.
    Raises ValueError if the URL is unsafe.
    """
    import socket

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Audio URL must use HTTP(S), got: {parsed.scheme}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Audio URL has no hostname")

    # Block obvious localhost variants
    if hostname in ("localhost", "0.0.0.0", "[::]"):
        if not IS_LOCAL:
            raise ValueError(f"Audio URL targets localhost: {hostname}")

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve audio URL hostname: {hostname}")

    validated: list[tuple[int, str]] = []
    for family, _, _, _, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if str(ip) == "169.254.169.254":
            raise ValueError("Audio URL targets AWS metadata endpoint")
        if ip.is_link_local:
            raise ValueError(f"Audio URL resolves to link-local: {ip}")
        if ip.is_loopback:
            if not IS_LOCAL:
                raise ValueError(f"Audio URL resolves to loopback: {ip}")
            validated.append((family, sockaddr[0]))
            continue  # loopback allowed in local — skip remaining checks
        if ip.is_private:
            if not IS_LOCAL:
                raise ValueError(f"Audio URL resolves to private IP: {ip}")
            validated.append((family, sockaddr[0]))
            continue  # private allowed in local — skip remaining checks
        if ip.is_reserved:
            raise ValueError(f"Audio URL resolves to reserved IP: {ip}")
        validated.append((family, sockaddr[0]))

    if not validated:
        raise ValueError(f"No valid addresses resolved for: {hostname}")
    return validated


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """Resolver returning pre-validated IPs to prevent DNS rebinding."""

    def __init__(self, host: str, addrs: list[tuple[int, str]]):
        self._host = host
        self._addrs = addrs

    async def resolve(self, host: str, port: int = 0, family: int = 0) -> list[dict]:
        if host != self._host:
            raise OSError(
                f"DNS resolution blocked: unexpected host {host!r} (pinned to {self._host!r})"
            )
        return [
            {
                "hostname": host,
                "host": ip,
                "port": port,
                "family": fam,
                "proto": 0,
                "flags": 0,
            }
            for fam, ip in self._addrs
            if family == 0 or family == fam
        ]

    async def close(self) -> None:
        pass


async def fetch_audio_from_url(
    url: str,
    target_sample_rate: int = 8000,
    target_channels: int = 1,
) -> Optional[bytes]:
    try:
        from pydub import AudioSegment
    except ImportError:
        logger.error(
            "pydub is required for audio URL playback. Install with: pip install pydub"
        )
        return None

    # SSRF validation — returns pinned IPs to prevent DNS rebinding
    try:
        validated_addrs = _validate_audio_url(url)
    except ValueError as e:
        logger.error(f"Audio URL validation failed: {e}")
        return None

    try:
        logger.info(f"Fetching audio from URL: {url}")
        parsed = urlparse(url)
        connector = aiohttp.TCPConnector(
            resolver=_PinnedResolver(parsed.hostname, validated_addrs)
        )
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
        ) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    logger.error(
                        f"Failed to fetch audio from {url}: status={response.status}"
                    )
                    return None

                # Check Content-Length before downloading
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_AUDIO_FETCH_BYTES:
                    logger.error(
                        f"Audio too large: {content_length} bytes "
                        f"(max {MAX_AUDIO_FETCH_BYTES})"
                    )
                    return None

                # Stream with size limit
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > MAX_AUDIO_FETCH_BYTES:
                        logger.error("Audio exceeded size limit during download")
                        return None
                    chunks.append(chunk)
                audio_data = b"".join(chunks)
                content_type = response.headers.get("Content-Type", "")
                logger.info(
                    f"Fetched audio: size={len(audio_data)} bytes, type={content_type}"
                )

        format_hint = None
        url_lower = url.lower()
        if ".mp3" in url_lower:
            format_hint = "mp3"
        elif ".wav" in url_lower:
            format_hint = "wav"
        elif ".ogg" in url_lower:
            format_hint = "ogg"
        elif ".m4a" in url_lower or ".aac" in url_lower:
            format_hint = "m4a"
        elif ".flac" in url_lower:
            format_hint = "flac"
        elif ".webm" in url_lower:
            format_hint = "webm"
        elif "audio/mpeg" in content_type:
            format_hint = "mp3"
        elif "audio/wav" in content_type or "audio/wave" in content_type:
            format_hint = "wav"
        elif "audio/ogg" in content_type:
            format_hint = "ogg"

        audio_buffer = io.BytesIO(audio_data)
        audio = (
            AudioSegment.from_file(audio_buffer, format=format_hint)
            if format_hint
            else AudioSegment.from_file(audio_buffer)
        )
        audio = (
            audio.set_frame_rate(target_sample_rate)
            .set_channels(target_channels)
            .set_sample_width(2)
        )
        raw_pcm = audio.raw_data
        logger.info(
            f"Audio converted: duration={len(audio)}ms, rate={target_sample_rate}, size={len(raw_pcm)} bytes"
        )
        return raw_pcm

    except Exception as e:
        logger.exception(f"Error fetching/converting audio from {url}: {e}")
        return None


def chunk_audio(
    audio_data: bytes, sample_rate: int, chunk_duration_ms: int = 20
) -> list[bytes]:
    bytes_per_chunk = int(sample_rate * 2 * chunk_duration_ms / 1000)
    chunks = [
        audio_data[i : i + bytes_per_chunk]
        for i in range(0, len(audio_data), bytes_per_chunk)
        if audio_data[i : i + bytes_per_chunk]
    ]
    logger.debug(
        f"Split audio into {len(chunks)} chunks of ~{bytes_per_chunk} bytes each"
    )
    return chunks


# =============================================================================
# Unified Pipeline Factory
# =============================================================================


async def create_pipeline(
    provider: WorkerProvider,
    websocket: WebSocket,
    call_sid: str,
    call_info: dict,
    custom_params: dict,
    phone_config: Optional[dict],
    assistant_config: dict,
    pre_warmed_stt=None,
    pre_warmed_tts=None,
    pre_warmed_vad=None,
    pre_warmed_smart_turn=None,
    pre_warmed_llm=None,
    pre_warmed_end_call=None,
    pre_warmed_context=None,
    pre_warmed_aggregators=None,
    metrics: Optional[CallMetrics] = None,
    org_id: str = "unknown",
) -> tuple[PipelineTask, object]:
    """Build and return (PipelineTask, transport) for the given provider.

    Phases:
    1. Build AssistantConfig
    2. Resolve audio params from provider
    3. Create and pre-warm STT/TTS services in parallel
    4. Create transport (provider-specific)
    5. Assemble pipeline + PipelineTask
    6. Register greeting/disconnect handlers
    """
    pipeline_start = time.monotonic()

    user_custom_params = {
        k: v for k, v in custom_params.items() if k not in SYSTEM_PARAM_KEYS
    }
    config = AssistantConfig(
        custom_params=custom_params,
        assistant_config=assistant_config,
        phone_config=phone_config,
        user_custom_params=user_custom_params,
    )

    in_rate, out_rate, encoding = provider.get_audio_params(call_info)

    # --- Phase 1: Create services (use pre-warmed if available) ---
    t0 = time.monotonic()
    # Serializers decode mulaw→PCM before handing audio to Deepgram, so
    # Deepgram always receives linear16 regardless of the wire encoding.
    stt_encoding = "linear16" if encoding == "mulaw" else encoding
    stt = (
        pre_warmed_stt
        if pre_warmed_stt is not None
        else create_stt_service(config, sample_rate=in_rate, encoding=stt_encoding)
    )
    tts = (
        pre_warmed_tts
        if pre_warmed_tts is not None
        else create_tts_service(config, sample_rate=out_rate)
    )
    if pre_warmed_llm is not None:
        llm = pre_warmed_llm
        logger.info(f"[{provider.name}] call_sid={call_sid}: using pre-warmed LLM")
    else:
        llm_params = BaseOpenAILLMService.InputParams(
            temperature=config.temperature,
            max_completion_tokens=config.max_completion_tokens,
            # Only pass service_tier when set — passing None causes an OpenAI API error.
            **(
                {"service_tier": config.service_tier}
                if config.service_tier is not None
                else {}
            ),
        )
        logger.info(
            f"Creating LLM: provider={config.llm_provider!r}, model={config.model!r}, "
            f"temperature={config.temperature}, "
            f"max_completion_tokens={config.max_completion_tokens}, "
            f"service_tier={config.service_tier!r}"
        )
        llm = OpenAILLMService(
            api_key=OPENAI_API_KEY, model=config.model, params=llm_params
        )
    if pre_warmed_end_call is not None:
        end_call_processor = pre_warmed_end_call
    else:
        end_call_processor = EndCallProcessor(config.end_call_phrases)
    if pre_warmed_vad is not None and pre_warmed_smart_turn is not None:
        vad_analyzer = pre_warmed_vad
        smart_turn_analyzer = pre_warmed_smart_turn
        logger.info(
            f"[{provider.name}] call_sid={call_sid}: using pre-warmed VAD + SmartTurn"
        )
    else:
        vad_cfg = config.vad_settings
        vad_params = VADParams(
            confidence=vad_cfg.get("confidence", 0.7),
            start_secs=vad_cfg.get("start_secs", 0.2),
            stop_secs=vad_cfg.get("stop_secs", 0.2),
            min_volume=vad_cfg.get("min_volume", 0.6),
        )
        smart_turn_params = SmartTurnParams(
            stop_secs=vad_cfg.get("smart_turn_stop_secs", 3.0),
            pre_speech_ms=vad_cfg.get("smart_turn_pre_speech_ms", 500),
            max_duration_secs=vad_cfg.get("smart_turn_max_duration_secs", 8.0),
        )
        logger.info(
            f"Creating VAD: confidence={vad_params.confidence}, start_secs={vad_params.start_secs}, "
            f"stop_secs={vad_params.stop_secs}, min_volume={vad_params.min_volume} | "
            f"SmartTurn: stop_secs={smart_turn_params.stop_secs}, "
            f"pre_speech_ms={smart_turn_params.pre_speech_ms}, "
            f"max_duration_secs={smart_turn_params.max_duration_secs}"
        )
        vad_analyzer = SileroVADAnalyzer(params=vad_params)
        smart_turn_analyzer = LocalSmartTurnAnalyzerV3(params=smart_turn_params)
    full_prewarm = all(
        x is not None
        for x in [
            pre_warmed_stt,
            pre_warmed_tts,
            pre_warmed_vad,
            pre_warmed_smart_turn,
            pre_warmed_llm,
            pre_warmed_end_call,
            pre_warmed_context,
            pre_warmed_aggregators,
        ]
    )
    logger.info(
        f"[{provider.name}] call_sid={call_sid}: services created in {(time.monotonic() - t0) * 1000:.1f}ms"
        + (f" (full_prewarm={full_prewarm})" if pre_warmed_stt else "")
    )

    # --- Phase 2: Pre-warm STT + TTS in parallel (skip already-warmed) ---
    t0 = time.monotonic()

    async def _prewarm(svc):
        if hasattr(svc, "is_prewarmed") and svc.is_prewarmed:
            return True  # Already warmed
        if hasattr(svc, "prewarm"):
            return await svc.prewarm()
        return True

    stt_ok, tts_ok = await asyncio.gather(_prewarm(stt), _prewarm(tts))
    logger.info(
        f"[{provider.name}] call_sid={call_sid}: pre-warm done in {(time.monotonic() - t0) * 1000:.1f}ms "
        f"(STT={stt_ok}, TTS={tts_ok})"
    )

    # --- Phase 3: Create transport ---
    transport = provider.create_transport(
        websocket, vad_analyzer, call_info, config, in_rate, out_rate
    )

    # --- Phase 4: Build pipeline ---
    if pre_warmed_context is not None and pre_warmed_aggregators is not None:
        context = pre_warmed_context
        messages = context.messages  # shared ref used by greeting handler
        user_aggregator, assistant_aggregator = pre_warmed_aggregators
        logger.info(
            f"[{provider.name}] call_sid={call_sid}: using pre-warmed Context + Aggregators"
        )
    else:
        messages = [{"role": "system", "content": config.get_system_message()}]
        context = LLMContext(messages)

        start_strategies = None
        if config.interruption_strategy == "llm_judge":
            start_strategies = [
                LLMInterruptionJudgeStrategy(
                    openai_api_key=OPENAI_API_KEY,
                )
            ]
        logger.info(
            f"[{provider.name}] call_sid={call_sid}: interruption_strategy={config.interruption_strategy}"
        )

        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                user_turn_strategies=UserTurnStrategies(
                    start=start_strategies,  # None → pipecat defaults
                    stop=[
                        TurnAnalyzerUserTurnStopStrategy(
                            turn_analyzer=smart_turn_analyzer
                        )
                    ],
                ),
            ),
        )

    pipeline_stages = [
        transport.input(),
        stt,
        user_aggregator,
        llm,
        end_call_processor,
        tts,
        transport.output(),
        assistant_aggregator,
    ]
    if metrics is not None:
        stt_system = (config.transcriber_provider or "deepgram").lower()
        pipeline_stages.insert(
            2,
            TranscriptionStatsProcessor(metrics, org_id=org_id, stt_system=stt_system),
        )
        pipeline_stages.append(
            MetricsProcessor(metrics, org_id=org_id, provider=provider.name)
        )
        # Wire metrics into LLM judge strategy for interrupt_llm_latency tracking
        try:
            strategies = (
                user_aggregator._user_turn_controller._user_turn_strategies.start or []
            )
            for s in strategies:
                if isinstance(s, LLMInterruptionJudgeStrategy):
                    s.set_call_metrics(metrics)
        except AttributeError:
            pass

    pipeline = Pipeline(pipeline_stages)

    # Build silence nudge observer before creating the task so it can be passed
    # as an observer. The pipeline task ref is wired in after task creation.
    silence_observer: Optional[SilenceNudgeObserver] = None
    if config.silence_response_enabled:
        silence_observer = SilenceNudgeObserver(
            config=config,
            call_sid=call_sid,
            provider_name=provider.name,
        )

    assistant_name = (assistant_config or {}).get("name", "")
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=in_rate,
            audio_out_sample_rate=out_rate,
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=[silence_observer] if silence_observer else None,
        enable_tracing=is_pipecat_tracing_enabled(),
        enable_turn_tracking=is_pipecat_tracing_enabled(),
        conversation_id=call_sid,
        additional_span_attributes={
            "langfuse.trace.name": call_sid,
            "langfuse.session.id": call_sid,
            "assistant_name": assistant_name[:200] if assistant_name else "",
            "provider": provider.name,
        }
        if is_pipecat_tracing_enabled()
        else None,
    )

    end_call_processor.set_task(task)

    # Wire the pipeline task into the silence observer now that it exists.
    if silence_observer:
        silence_observer.set_pipeline_task(task)

    pipeline_ready_at = time.monotonic()
    if metrics is not None:
        metrics.record_pipeline_ready(pipeline_ready_at)

    logger.info(
        f"[{provider.name}] call_sid={call_sid}: pipeline ready in "
        f"{(pipeline_ready_at - pipeline_start) * 1000:.1f}ms"
        + (
            f", end_call_phrases={config.end_call_phrases}"
            if config.end_call_phrases
            else ""
        )
    )

    # --- Greeting handler ---
    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        if metrics is not None:
            metrics.record_client_connected()
        logger.info(f"[{provider.name}] call_sid={call_sid}: client connected")

        if not config.bot_speaks_first:
            logger.info(
                f"[{provider.name}] call_sid={call_sid}: bot_speaks_first=False — skipping greeting, waiting for user"
            )
            return

        greeting = config.get_greeting()
        if not greeting:
            logger.info(
                f"[{provider.name}] call_sid={call_sid}: no greeting configured"
            )
            return

        logger.debug(
            f"[{provider.name}] call_sid={call_sid}: greeting (first 100): {greeting[:100]}"
        )

        if is_audio_url(greeting):
            logger.info(
                f"[{provider.name}] call_sid={call_sid}: fetching audio greeting from URL"
            )
            try:
                audio_data = await fetch_audio_from_url(
                    greeting, target_sample_rate=out_rate
                )
                if audio_data:
                    messages.append(
                        {"role": "assistant", "content": "[Audio greeting played]"}
                    )
                    chunks = chunk_audio(
                        audio_data, sample_rate=out_rate, chunk_duration_ms=20
                    )
                    frames = [
                        OutputAudioRawFrame(
                            audio=c, sample_rate=out_rate, num_channels=1
                        )
                        for c in chunks
                    ]
                    logger.info(
                        f"[{provider.name}] call_sid={call_sid}: queuing {len(frames)} audio frames"
                    )
                    await task.queue_frames(frames)
                else:
                    logger.warning(
                        f"[{provider.name}] call_sid={call_sid}: audio fetch failed, falling back to TTS"
                    )
                    messages.append(
                        {"role": "assistant", "content": "Hello, how can I help you?"}
                    )
                    await task.queue_frames(
                        [TTSSpeakFrame("Hello, how can I help you?")]
                    )
            except Exception as e:
                logger.exception(
                    f"[{provider.name}] call_sid={call_sid}: audio URL error: {e}"
                )
                messages.append(
                    {"role": "assistant", "content": "Hello, how can I help you?"}
                )
                await task.queue_frames([TTSSpeakFrame("Hello, how can I help you?")])
        else:
            logger.info(f"[{provider.name}] call_sid={call_sid}: sending TTS greeting")
            messages.append({"role": "assistant", "content": greeting})
            await task.queue_frames([TTSSpeakFrame(greeting)])
        if metrics is not None:
            metrics.record_greeting_queued()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        if metrics is not None:
            metrics.record_client_disconnected()
        logger.info(f"[{provider.name}] call_sid={call_sid}: client disconnected")
        await task.queue_frames([EndFrame()])

    return task, transport
