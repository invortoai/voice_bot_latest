import asyncio
import hmac
import time
from contextlib import asynccontextmanager
from typing import Optional, Tuple

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.runner import PipelineRunner
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContext,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.openai.base_llm import BaseOpenAILLMService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pydantic import BaseModel

from app.config import (
    ENABLE_CALL_METRICS,
    ENVIRONMENT,
    IS_LOCAL,
    OPENAI_API_KEY,
    WORKER_AUTH_TOKEN,
    WORKER_PUBLIC_WS_SCHEME,
    WS_ALLOWED_ORIGINS,
)
from app.core.log_setup import setup_logging
from app.worker.strategies.llm_interruption_judge import LLMInterruptionJudgeStrategy
from app.core.context import set_log_context, set_span_attrs as _set_span_attrs
from app.observability.otel import setup_otel
from app.core.tracing import traced, register_library_instrumentors
from app.services import call_service, phone_number_service
from app.worker.assistant_service import get_assistant_by_id
from app.worker.config import AssistantConfig, SYSTEM_PARAM_KEYS
from app.worker.metrics import CallMetrics
from app.worker.pipeline import create_pipeline
from app.worker.processors.end_call import EndCallProcessor
from app.worker.prewarm import (
    PrewarmEntry,
    _PROVIDER_DEFAULT_AUDIO_PARAMS,
    prewarm_cache,
)
from app.worker.providers.base import WorkerProvider
from app.worker.providers.jambonz import JambonzProvider
from app.worker.providers.mcube import McubeProvider
from app.worker.providers.twilio import TwilioProvider
from app.worker.pipecat_tracing import setup_pipecat_langfuse_tracing
from app.worker.services import create_stt_service, create_tts_service
from app.worker.state import worker_state
from app.worker.call_events import emit_call_started, emit_call_completed
from app.worker.otel_metrics import record_call_start, record_call_end, record_prewarm
from app.observability.utils import safe_observe




# ── Worker management endpoint authentication ────────────────────────────────


async def verify_worker_auth(
    x_worker_auth: Optional[str] = Header(default=None, alias="X-Worker-Auth"),
) -> None:
    """Require X-Worker-Auth header on management endpoints.

    Raises 503 if WORKER_AUTH_TOKEN is not configured.
    Raises 403 if header is missing or token doesn't match.
    """
    if not WORKER_AUTH_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Worker authentication not configured",
        )
    if not x_worker_auth or not hmac.compare_digest(x_worker_auth, WORKER_AUTH_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid worker auth token")


def _check_ws_origin(websocket: WebSocket) -> bool:
    """Check WebSocket Origin header against allowed list.

    Returns True if the connection should be accepted:
    - If no Origin header (server-to-server, e.g. Twilio): True
    - If WS_ALLOWED_ORIGINS is empty: True (opt-in enforcement)
    - If WS_ALLOWED_ORIGINS is configured and Origin matches: True
    - If WS_ALLOWED_ORIGINS is configured and Origin doesn't match: False

    When WS_ALLOWED_ORIGINS is not configured, origin validation is not enforced.
    Workers are protected by security groups (DAAI-136), audio protocol validation,
    and call_sid matching. Origin check is opt-in defense-in-depth.
    """
    client = websocket.client
    client_info = f"{client.host}:{client.port}" if client else "unknown"
    path = websocket.url.path
    logger.info(
        f"[ws-origin] WebSocket connection from {client_info} to {path} — "
        f"headers: {dict(websocket.headers)}"
    )

    origin = websocket.headers.get("origin", "")

    if not origin:
        logger.info(
            f"[ws-origin] {path} from {client_info}: no Origin header — allowing (server-to-server)"
        )
        return True

    if not WS_ALLOWED_ORIGINS:
        logger.info(
            f"[ws-origin] {path} from {client_info}: Origin={origin!r} — "
            f"allowing (WS_ALLOWED_ORIGINS not configured)"
        )
        return True

    origin_lower = origin.lower().rstrip("/")
    allowed = any(origin_lower == a.lower().rstrip("/") for a in WS_ALLOWED_ORIGINS)

    if allowed:
        logger.info(
            f"[ws-origin] {path} from {client_info}: Origin={origin!r} — allowed"
        )
    else:
        logger.warning(
            f"[ws-origin] {path} from {client_info}: Origin={origin!r} not in "
            f"WS_ALLOWED_ORIGINS={WS_ALLOWED_ORIGINS} — rejecting"
        )
    return allowed


async def _prewarm_ttl_cleanup():
    """Periodically evict expired prewarm entries and cancel their tasks."""
    while True:
        await asyncio.sleep(30)
        try:
            expired = await prewarm_cache.evict_expired()
            for entry in expired:
                entry.is_cancelled = True
                if entry.task and not entry.task.done():
                    # Task still running — CancelledError handler will close stt/tts
                    entry.task.cancel()
                elif entry.is_ready:
                    # Task already finished — open connections must be closed directly
                    asyncio.create_task(_close_prewarm_services(entry.stt, entry.tts))
            if expired:
                logger.debug(f"[prewarm] evicted {len(expired)} expired entries")
        except Exception as e:
            logger.error(f"[prewarm] TTL cleanup error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging("worker", environment=ENVIRONMENT)
    setup_otel(service_name="invorto-worker", environment=ENVIRONMENT)
    register_library_instrumentors()
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="/health")
    except ImportError:
        pass
    logger.info("Bot worker starting up...")
    setup_pipecat_langfuse_tracing()
    logger.info("Pre-loading Silero VAD model...")
    _ = SileroVADAnalyzer()
    cleanup_task = asyncio.create_task(_prewarm_ttl_cleanup())
    if not IS_LOCAL and WORKER_PUBLIC_WS_SCHEME != "wss":
        logger.warning(
            "SECURITY: Worker WebSocket not configured for TLS "
            f"(WORKER_PUBLIC_WS_SCHEME={WORKER_PUBLIC_WS_SCHEME!r}). "
            "Voice audio may be transmitted unencrypted."
        )
    logger.info("Bot worker ready")
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("Bot worker shutting down...")


app = FastAPI(title="Invorto AI Bot Worker", lifespan=lifespan)


# =============================================================================
# Shared Config Resolution
# =============================================================================


async def _fetch_config_from_call_record(
    call_sid: str,
) -> Tuple[Optional[dict], Optional[dict], Optional[dict]]:
    """Fetch (call_record, phone_config, assistant_config) from the database.

    Fetches call record first to get assistant_id and phone_number_id,
    then fetches assistant and phone configs in parallel.
    Returns (None, None, None) if call record not found or on error.
    """
    try:
        call_record = await asyncio.to_thread(call_service.get_by_sid, call_sid)
        if not call_record:
            logger.debug(f"No call record found for call_sid={call_sid}")
            return None, None, None

        assistant_id = call_record.get("assistant_id")
        phone_number_id = call_record.get("phone_number_id")

        async def _get_assistant():
            if assistant_id:
                return await asyncio.to_thread(get_assistant_by_id, str(assistant_id))
            return None

        async def _get_phone():
            if phone_number_id:
                return await asyncio.to_thread(
                    phone_number_service.get_by_id, str(phone_number_id)
                )
            return None

        assistant_config, phone_config = await asyncio.gather(
            _get_assistant(), _get_phone()
        )

        if assistant_config:
            logger.info(
                f"Loaded config from call record: call_sid={call_sid}, "
                f"assistant={assistant_config.get('name')}",
                extra={"call_sid": call_sid},
            )
        else:
            logger.warning(
                f"Call record found but assistant config missing: call_sid={call_sid}, "
                f"assistant_id={assistant_id}",
                extra={"call_sid": call_sid},
            )

        return call_record, phone_config, assistant_config

    except Exception as e:
        logger.error(
            f"Error fetching call record for call_sid={call_sid}: {e}",
            exc_info=True,
            extra={"call_sid": call_sid},
        )
        return None, None, None


def _build_custom_params_from_record(call_sid: str, call_record: dict) -> dict:
    result = {
        "call_sid": call_sid,
        "call_type": call_record.get("direction", "inbound"),
        "caller": call_record.get("from_number", ""),
        "called": call_record.get("to_number", ""),
        "to_number": call_record.get("to_number", ""),
    }
    # Merge user custom_params stored in the calls table (JSONB column).
    # Filter SYSTEM_PARAM_KEYS to prevent user-supplied values from overwriting
    # system-controlled fields like call_type, call_sid, etc.
    user_params = call_record.get("custom_params", {})
    if isinstance(user_params, dict):
        safe_params = {
            k: v for k, v in user_params.items() if k not in SYSTEM_PARAM_KEYS
        }
        result.update(safe_params)
    return result


# =============================================================================
# Pre-warm Background Task
# =============================================================================


async def _close_prewarm_services(stt, tts) -> None:
    for svc in [stt, tts]:
        if svc and hasattr(svc, "close_prewarm"):
            try:
                await svc.close_prewarm()
            except Exception as e:
                logger.warning(f"[prewarm] error closing prewarm service: {e}")


async def _fill_prewarm(
    call_sid: str, entry: PrewarmEntry, config_payload: Optional[dict] = None
) -> None:
    """Background task: fetch call config, create services, and pre-warm connections.

    When config_payload is provided (sent by runner), skips the DB fetch entirely.
    config_payload keys: assistant_config, phone_config, custom_params, provider_name.
    """
    stt = None
    tts = None
    try:
        _t0 = time.monotonic()
        _prev = _t0
        pw = {}  # prewarm metrics dict

        call_record = None
        if config_payload:
            assistant_config = config_payload["assistant_config"]
            phone_config = config_payload.get("phone_config") or {}
            custom_params = config_payload.get("custom_params") or {}
            provider_name = config_payload.get("provider_name") or "jambonz"
            pw["config_source"] = "payload"
            logger.debug(
                f"[prewarm] call_sid={call_sid}: config from payload (no DB fetch)"
            )
        else:
            (
                call_record,
                phone_config,
                assistant_config,
            ) = await _fetch_config_from_call_record(call_sid)

            if not call_record or not assistant_config:
                logger.warning(
                    f"[prewarm] call_sid={call_sid}: config not found, skipping"
                )
                return
            provider_name = call_record.get("provider", "twilio")
            custom_params = _build_custom_params_from_record(call_sid, call_record)
            pw["config_source"] = "db"

        _now = time.monotonic()
        pw["config_resolved_ms"] = round((_now - _prev) * 1000, 1)
        _prev = _now

        if not assistant_config:
            logger.warning(f"[prewarm] call_sid={call_sid}: config not found, skipping")
            return

        # Cache org_id so _handle_call attributes OTEL metrics correctly on a
        # full cache hit (DB fetch is skipped entirely in that path).
        if config_payload:
            entry.org_id = str((phone_config or {}).get("org_id") or "unknown")
        else:
            entry.org_id = str((call_record or {}).get("org_id") or "unknown")

        # Store config in entry for _handle_call to use directly
        entry.assistant_config = assistant_config
        entry.phone_config = phone_config
        entry.custom_params = custom_params

        in_rate, out_rate, encoding = _PROVIDER_DEFAULT_AUDIO_PARAMS.get(
            provider_name, (8000, 8000, "mulaw")
        )
        entry.in_rate = in_rate
        entry.out_rate = out_rate
        entry.encoding = encoding

        config = AssistantConfig(
            custom_params=custom_params,
            assistant_config=assistant_config,
            phone_config=phone_config,
        )

        # --- STT ---
        stt_encoding = "linear16" if encoding == "mulaw" else encoding
        stt = create_stt_service(config, sample_rate=in_rate, encoding=stt_encoding)
        _now = time.monotonic()
        pw["stt_created_ms"] = round((_now - _prev) * 1000, 1)
        _prev = _now

        # --- TTS ---
        tts = create_tts_service(config, sample_rate=out_rate)
        _now = time.monotonic()
        pw["tts_created_ms"] = round((_now - _prev) * 1000, 1)
        _prev = _now

        entry.stt = stt
        entry.tts = tts

        # --- VAD ---
        vad_cfg = config.vad_settings
        vad_params = VADParams(
            confidence=vad_cfg.get("confidence", 0.7),
            start_secs=vad_cfg.get("start_secs", 0.2),
            stop_secs=vad_cfg.get("stop_secs", 0.8),
            min_volume=vad_cfg.get("min_volume", 0.6),
        )
        entry.vad_analyzer = SileroVADAnalyzer(params=vad_params)
        _now = time.monotonic()
        pw["vad_created_ms"] = round((_now - _prev) * 1000, 1)
        _prev = _now

        # --- SmartTurn ---
        smart_turn_params = SmartTurnParams(
            stop_secs=vad_cfg.get("smart_turn_stop_secs", 3.0),
            pre_speech_ms=vad_cfg.get("smart_turn_pre_speech_ms", 500),
            max_duration_secs=vad_cfg.get("smart_turn_max_duration_secs", 8.0),
        )
        entry.smart_turn_analyzer = LocalSmartTurnAnalyzerV3(params=smart_turn_params)
        _now = time.monotonic()
        pw["smart_turn_created_ms"] = round((_now - _prev) * 1000, 1)
        _prev = _now

        # --- LLM ---
        llm_params = BaseOpenAILLMService.InputParams(
            temperature=config.temperature,
            max_completion_tokens=config.max_completion_tokens,
            service_tier=config.service_tier,
        )
        entry.llm = OpenAILLMService(
            api_key=OPENAI_API_KEY, model=config.model, params=llm_params
        )
        _now = time.monotonic()
        pw["llm_created_ms"] = round((_now - _prev) * 1000, 1)
        _prev = _now

        # --- EndCallProcessor ---
        entry.end_call_processor = EndCallProcessor(config.end_call_phrases)
        _now = time.monotonic()
        pw["end_call_created_ms"] = round((_now - _prev) * 1000, 1)
        _prev = _now

        # --- LLMContext + Aggregators ---
        messages = [{"role": "system", "content": config.get_system_message()}]
        entry.context = LLMContext(messages)

        start_strategies = None
        if config.interruption_strategy == "llm_judge":
            start_strategies = [
                LLMInterruptionJudgeStrategy(
                    openai_api_key=OPENAI_API_KEY,
                )
            ]
        logger.info(
            f"[prewarm] call_sid={call_sid}: interruption_strategy={config.interruption_strategy}"
        )

        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            entry.context,
            user_params=LLMUserAggregatorParams(
                user_turn_strategies=UserTurnStrategies(
                    start=start_strategies,  # None → pipecat defaults
                    stop=[
                        TurnAnalyzerUserTurnStopStrategy(
                            turn_analyzer=entry.smart_turn_analyzer
                        )
                    ],
                ),
            ),
        )
        entry.context_aggregator = (user_aggregator, assistant_aggregator)
        _now = time.monotonic()
        pw["context_created_ms"] = round((_now - _prev) * 1000, 1)

        # --- Parallel STT + TTS warm ---
        async def _timed_prewarm(svc, label):
            t = time.monotonic()
            result = await svc.prewarm()
            return label, round((time.monotonic() - t) * 1000, 1), result

        _warm_start = time.monotonic()
        warm_results = await asyncio.gather(
            _timed_prewarm(stt, "stt"),
            _timed_prewarm(tts, "tts"),
            return_exceptions=True,
        )
        pw["warm_parallel_ms"] = round((time.monotonic() - _warm_start) * 1000, 1)

        stt_ok = tts_ok = False
        for r in warm_results:
            if isinstance(r, tuple):
                label, dur_ms, ok = r
                pw[f"{label}_warmed_ms"] = dur_ms
                if label == "stt":
                    stt_ok = ok is True
                elif label == "tts":
                    tts_ok = ok is True

        pw["total_ms"] = round((time.monotonic() - _t0) * 1000, 1)
        entry.prewarm_metrics = pw

        if entry.is_cancelled:
            await _close_prewarm_services(stt, tts)
            logger.debug(
                f"[prewarm] call_sid={call_sid}: cancelled during prewarm, connections closed"
            )
            return

        entry.is_ready = stt_ok and tts_ok
        logger.info(
            f"[prewarm] call_sid={call_sid}: {'ready' if entry.is_ready else 'NOT ready'} in {pw['total_ms']:.0f}ms "
            f"(config={pw['config_source']}, stt={stt_ok}, tts={tts_ok})"
        )

    except asyncio.CancelledError:
        entry.is_cancelled = True
        await _close_prewarm_services(stt, tts)
        logger.debug(f"[prewarm] call_sid={call_sid}: task cancelled")
        raise
    except Exception as e:
        logger.error(f"[prewarm] call_sid={call_sid}: error: {e}", exc_info=True)


# =============================================================================
# Unified Call Handler
# =============================================================================


async def _save_metrics_with_log(call_sid: str, metrics_dict: dict) -> None:
    """Save call metrics; log any failure so it is never silently dropped."""
    try:
        await asyncio.to_thread(call_service.save_metrics, call_sid, metrics_dict)
    except Exception as e:
        logger.error(
            f"[metrics] failed to save metrics for call_sid={call_sid}: {e}",
            exc_info=True,
        )


@traced(name="invorto.call")
async def _handle_call(
    websocket: WebSocket,
    provider: WorkerProvider,
    path_call_sid: Optional[str] = None,
    ws_accepted_at: Optional[float] = None,
) -> None:
    """Unified WebSocket handler for all telephony providers.

    Flow:
    1. Parse provider-specific initial message(s)
    2. Extract call_sid
    3. Fetch config from call record (DB-first, parallel fetches)
    4. Provider-specific fallback if call record not available
    5. Build AssistantConfig custom_params
    6. Start worker state, create and run pipeline
    """
    if ws_accepted_at is None:
        ws_accepted_at = time.monotonic()
    ws_accepted_wall = time.time()  # wall clock for transport_hop calculation
    call_sid: Optional[str] = None
    org_id: str = "unknown"
    custom_params: dict = {}
    task = None
    metrics: Optional[CallMetrics] = None
    _ended_by = "bot"
    _error_type: Optional[str] = None

    try:
        # Step 1 + 2: Parse initial message and extract call_sid
        call_info = await provider.parse_initial_message(websocket, path_call_sid)
        msg_parsed_at = time.monotonic()
        call_sid = provider.extract_call_sid(call_info, path_call_sid)
        set_log_context(call_sid=call_sid, provider=provider.name.lower())
        _set_span_attrs(**{"call_sid": call_sid, "telephony.provider": provider.name.lower()})
        logger.info(f"[{provider.name}] call_sid={call_sid}: WebSocket call started")

        # Mark call as active immediately after extracting call_sid.
        # This keeps worker_state.current_call visible during the startup window
        # (config fetch, pipeline build) so the runner's health-check body reader
        # never sees current_call=null while a call is genuinely in progress.
        # end_call() is always called in the finally block regardless of how we exit.
        await worker_state.start_call(call_sid)

        # Extract runner timing from metadata (injected by jambonz.py)
        _runner_webhook_ms = call_info.get("runner_webhook_ms")
        _webhook_completed_at = call_info.get("webhook_completed_at")
        _transport_hop_ms = None
        if _webhook_completed_at:
            _transport_hop_ms = round(
                (ws_accepted_wall - _webhook_completed_at) * 1000, 1
            )
            # Inter-instance clock skew (typically 1–50 ms on AWS) can produce a
            # negative value. Clip to zero rather than corrupting total_ms.
            if _transport_hop_ms < 0:
                logger.debug(
                    f"[metrics] transport_hop_ms={_transport_hop_ms:.1f} negative "
                    "(runner/worker clock skew) — clamped to 0"
                )
                _transport_hop_ms = 0.0

        # Step 3: Check prewarm cache FIRST (before any DB fetch)
        pre_entry: Optional[PrewarmEntry] = None
        pre_warmed_stt = pre_warmed_tts = None

        entry = await prewarm_cache.get(call_sid)
        if entry:
            # Wait up to 1s for the background task to finish
            if not entry.is_ready and entry.task and not entry.task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(entry.task), timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass

            if entry.is_ready and not entry.is_cancelled:
                in_rate, _, encoding = provider.get_audio_params(call_info)
                if entry.in_rate == in_rate and entry.encoding == encoding:
                    pre_entry = entry
                    pre_warmed_stt = entry.stt
                    pre_warmed_tts = entry.tts
                    logger.info(
                        f"[{provider.name}] call_sid={call_sid}: using pre-warmed services "
                        f"(full={entry.llm is not None})"
                    )
                else:
                    logger.warning(
                        f"[{provider.name}] call_sid={call_sid}: pre-warm params mismatch "
                        f"(entry: {entry.in_rate}/{entry.encoding} vs "
                        f"actual: {in_rate}/{encoding}), falling back"
                    )
                    # Services are open but can't be reused — close them
                    asyncio.create_task(_close_prewarm_services(entry.stt, entry.tts))
            else:
                logger.debug(
                    f"[{provider.name}] call_sid={call_sid}: pre-warm not ready, falling back"
                )
                # Cancel the still-running task — its result will never be used
                if entry.task and not entry.task.done():
                    entry.is_cancelled = True
                    entry.task.cancel()
            await prewarm_cache.remove(call_sid)

        # Step 4: Config resolution — from prewarm cache or DB
        if pre_entry and pre_entry.assistant_config:
            # Full cache hit — skip DB entirely.
            # org_id was cached in _fill_prewarm so metrics are attributed correctly
            # even without a DB fetch.
            org_id = pre_entry.org_id or "unknown"
            set_log_context(org_id=org_id)
            _set_span_attrs(org_id=org_id)
            phone_config = pre_entry.phone_config
            assistant_config = pre_entry.assistant_config
            # Build real call details from provider (call_sid, call_type, caller, called)
            # then merge cached prewarm params on top (assistant settings etc.)
            custom_params = provider.build_custom_params(call_sid, call_info)
            custom_params.update(pre_entry.custom_params or {})
        else:
            # Cache miss or no config — DB fetch
            (
                call_record,
                phone_config,
                assistant_config,
            ) = await _fetch_config_from_call_record(call_sid)

            if call_record and call_record.get("org_id"):
                org_id = str(call_record["org_id"])
                set_log_context(org_id=org_id)
                _set_span_attrs(org_id=org_id)

            # Provider fallback if DB lookup missed
            if not assistant_config:
                logger.info(
                    f"[{provider.name}] call_sid={call_sid}: call record lookup insufficient, "
                    "using provider fallback",
                )
                phone_config, assistant_config = await provider.config_fallback(
                    call_sid, call_info
                )

            if not assistant_config:
                logger.error(
                    f"[{provider.name}] call_sid={call_sid}: no assistant configuration found",
                )
                await websocket.close(code=1008, reason="Configuration not found")
                return

            # Build custom_params for AssistantConfig
            if call_record:
                custom_params = _build_custom_params_from_record(call_sid, call_record)
            else:
                custom_params = provider.build_custom_params(call_sid, call_info)

        config_resolved_at = time.monotonic()

        logger.info(
            f"[{provider.name}] call_sid={call_sid}: config resolved — "
            f"assistant={assistant_config.get('name')}",
        )

        # Step 6: Create pipeline (worker_state.start_call already called above)
        safe_observe(
            record_call_start,
            org_id=org_id,
            provider=provider.name,
            call_type=custom_params.get("call_type", "inbound"),
        )
        safe_observe(
            emit_call_started,
            call_sid=call_sid,
            org_id=org_id,
            provider=provider.name,
            caller=custom_params.get("caller", ""),
            callee=custom_params.get("called", ""),
            call_type=custom_params.get("call_type", "inbound"),
        )

        if ENABLE_CALL_METRICS:
            _ac = assistant_config or {}
            metrics = CallMetrics(
                ws_accepted_at=ws_accepted_at,
                stt_provider=_ac.get("transcriber_provider") or "deepgram",
                stt_model=_ac.get("transcriber_model") or "nova-2",
                stt_language=_ac.get("transcriber_language") or "en",
                llm_provider=_ac.get("llm_provider") or "openai",
                llm_model=_ac.get("model") or "",
                tts_provider=_ac.get("voice_provider") or "elevenlabs",
                tts_model=_ac.get("voice_model") or "",
                tts_voice_id=_ac.get("voice_id") or "",
            )
            if pre_entry and pre_entry.prewarm_metrics:
                metrics.set_prewarm_metrics(pre_entry.prewarm_metrics)
            # Initial latency breakdown: runner + transport + worker steps
            if _runner_webhook_ms is not None:
                metrics.set_runner_webhook_ms(_runner_webhook_ms)
            if _transport_hop_ms is not None:
                metrics.set_transport_hop_ms(_transport_hop_ms)
            metrics._ws_msg_recv_at = msg_parsed_at
            metrics._config_resolve_at = config_resolved_at
            metrics.record_prewarm_used(
                stt=pre_warmed_stt is not None,
                tts=pre_warmed_tts is not None,
            )

        task, transport = await create_pipeline(
            provider=provider,
            websocket=websocket,
            call_sid=call_sid,
            call_info=call_info,
            custom_params=custom_params,
            phone_config=phone_config,
            assistant_config=assistant_config,
            pre_warmed_stt=pre_warmed_stt,
            pre_warmed_tts=pre_warmed_tts,
            pre_warmed_vad=pre_entry.vad_analyzer if pre_entry else None,
            pre_warmed_smart_turn=pre_entry.smart_turn_analyzer if pre_entry else None,
            pre_warmed_llm=pre_entry.llm if pre_entry else None,
            pre_warmed_end_call=pre_entry.end_call_processor if pre_entry else None,
            pre_warmed_context=pre_entry.context if pre_entry else None,
            pre_warmed_aggregators=pre_entry.context_aggregator if pre_entry else None,
            metrics=metrics,
            org_id=org_id,
        )

        worker_state.active_task = task

        runner = PipelineRunner(handle_sigint=False)
        await runner.run(task)

    except WebSocketDisconnect:
        _ended_by = "user"
        logger.info(
            f"[{provider.name}] call_sid={call_sid or 'unknown'}: WebSocket disconnected by client"
        )
    except asyncio.TimeoutError:
        _ended_by = "timeout"
        _error_type = "timeout"
        logger.error(
            f"[{provider.name}] call_sid={call_sid or 'unknown'}: timeout during WebSocket handling"
        )
    except Exception as e:
        _ended_by = "error"
        _error_type = type(e).__name__
        logger.error(
            f"[{provider.name}] call_sid={call_sid or 'unknown'}: unhandled error: {e}",
            exc_info=True,
        )
    finally:
        if task:
            try:
                await task.cancel()
            except Exception:
                pass

        await worker_state.end_call()

        if call_sid and metrics is not None:
            metrics.record_call_ended(_ended_by, _error_type)
            # Serialise once — the same dict goes to OTEL metrics, the log event,
            # and DB save so all three consumers see identical data.
            _metrics_dict = metrics.to_dict()
            safe_observe(
                record_call_end,
                _metrics_dict,
                org_id=org_id,
                provider=provider.name,
                call_type=custom_params.get("call_type", "inbound"),
            )
            if metrics and metrics._prewarm_metrics:
                safe_observe(record_prewarm, metrics._prewarm_metrics, org_id=org_id)
            safe_observe(
                emit_call_completed,
                call_sid=call_sid,
                org_id=org_id,
                provider=provider.name,
                d=_metrics_dict,
                caller=custom_params.get("caller", ""),
                callee=custom_params.get("called", ""),
                call_type=custom_params.get("call_type", "inbound"),
            )
            asyncio.create_task(_save_metrics_with_log(call_sid, _metrics_dict))

        try:
            await websocket.close()
        except Exception:
            pass

        logger.info(
            f"[{provider.name}] call_sid={call_sid or 'unknown'}: handler completed"
        )


# =============================================================================
# WebSocket Route Handlers
# =============================================================================


@app.websocket("/ws")
async def ws_twilio(websocket: WebSocket):
    if not _check_ws_origin(websocket):
        await websocket.close(code=4003, reason="Origin not allowed")
        return
    await websocket.accept()
    ws_accepted_at = time.monotonic()
    logger.info("[twilio] WebSocket connection accepted")
    await _handle_call(websocket, TwilioProvider(), ws_accepted_at=ws_accepted_at)


@app.websocket("/ws/jambonz")
async def ws_jambonz(websocket: WebSocket):
    if not _check_ws_origin(websocket):
        await websocket.close(code=4003, reason="Origin not allowed")
        return
    await websocket.accept(subprotocol="audio.jambonz.org")
    ws_accepted_at = time.monotonic()
    logger.info("[jambonz] WebSocket connection accepted")
    await _handle_call(websocket, JambonzProvider(), ws_accepted_at=ws_accepted_at)


@app.websocket("/ws/mcube/{call_sid}")
async def ws_mcube(websocket: WebSocket, call_sid: str):
    if not _check_ws_origin(websocket):
        await websocket.close(code=4003, reason="Origin not allowed")
        return
    logger.debug(
        f"[mcube] WebSocket connection request: call_sid={call_sid}",
        extra={"call_sid": call_sid},
    )
    await websocket.accept()
    ws_accepted_at = time.monotonic()
    logger.info(
        f"[mcube] WebSocket connection accepted: call_sid={call_sid}",
        extra={"call_sid": call_sid},
    )
    await _handle_call(
        websocket,
        McubeProvider(),
        path_call_sid=call_sid,
        ws_accepted_at=ws_accepted_at,
    )


# =============================================================================
# Management Endpoints
# =============================================================================


class _PrewarmRequest(BaseModel):
    call_sid: str
    # Full config from runner — when provided, worker skips DB fetch
    assistant_config: Optional[dict] = None
    phone_config: Optional[dict] = None
    custom_params: Optional[dict] = None
    provider_name: Optional[str] = None
    # If True, block until prewarm is done and return {"status": "ready"|"not_ready"}
    wait: bool = False


class _PrewarmReassignRequest(BaseModel):
    old_key: str
    new_key: str


@app.post("/prewarm")
async def prewarm_call(body: _PrewarmRequest, _: None = Depends(verify_worker_auth)):
    """Trigger pre-warming of STT/TTS for an upcoming call.

    When assistant_config is provided, worker skips DB fetch.
    When wait=True, blocks until ready and returns {"status": "ready"}.
    """
    call_sid = body.call_sid
    existing = await prewarm_cache.get(call_sid)
    if existing:
        if body.wait and not existing.is_ready:
            if existing.task and not existing.task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(existing.task), timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
        return {"status": "ready" if existing.is_ready else "already_prewarming"}

    config_payload = None
    if body.assistant_config:
        config_payload = {
            "assistant_config": body.assistant_config,
            "phone_config": body.phone_config or {},
            "custom_params": body.custom_params or {},
            "provider_name": body.provider_name or "jambonz",
        }

    entry = PrewarmEntry(call_sid=call_sid)
    entry.task = asyncio.create_task(_fill_prewarm(call_sid, entry, config_payload))
    await prewarm_cache.put(entry)
    logger.info(
        f"[prewarm] call_sid={call_sid}: started "
        f"(config_from_payload={config_payload is not None}, wait={body.wait})"
    )

    if body.wait:
        try:
            await asyncio.wait_for(asyncio.shield(entry.task), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception) as e:
            logger.warning(f"[prewarm] call_sid={call_sid}: wait timed out: {e}")
        return {"status": "ready" if entry.is_ready else "not_ready"}

    return {"status": "prewarming"}


@app.post("/prewarm/reassign")
async def reassign_prewarm(
    body: _PrewarmReassignRequest, _: None = Depends(verify_worker_auth)
):
    """Re-key a prewarm cache entry from call_id to real call_sid after dial.

    Also updates worker_state.current_call_sid via compare-and-set so the
    runner's health-check body reader sees the new SID immediately, without
    waiting for the new WebSocket to arrive and call start_call().

    Compare-and-set semantics: only writes if current_call_sid == old_key.
    Idempotent: pushing the same new_key twice is a no-op.
    """
    ok = await prewarm_cache.reassign(body.old_key, body.new_key)

    # Update worker state with compare-and-set.  Only write if the worker is
    # currently tracking old_key — prevents a stale push from regressing the
    # SID to an old value if it arrives after the new WS has already advanced
    # the state (e.g. double-reassign or retry).
    async with worker_state._lock:
        if worker_state.current_call_sid == body.old_key:
            worker_state.current_call_sid = body.new_key
            logger.info(
                f"[prewarm/reassign] call_sid updated: {body.old_key} → {body.new_key}"
            )
        elif worker_state.current_call_sid == body.new_key:
            logger.debug(
                f"[prewarm/reassign] worker_state already has {body.new_key} — no-op"
            )
        else:
            logger.warning(
                f"[prewarm/reassign] CAS skipped: current={worker_state.current_call_sid!r} "
                f"doesn't match old_key={body.old_key!r} — push arrived out of order"
            )

    if ok:
        logger.debug(f"[prewarm] reassigned {body.old_key} → {body.new_key}")
        return {"status": "ok"}
    logger.warning(f"[prewarm] reassign not_found: {body.old_key} → {body.new_key}")
    return {"status": "not_found"}


@app.delete("/prewarm/{call_sid}")
async def cancel_prewarm(call_sid: str, _: None = Depends(verify_worker_auth)):
    """Cancel pre-warming for a call (e.g. worker released before WebSocket connected)."""
    entry = await prewarm_cache.remove(call_sid)
    if not entry:
        return {"status": "not_found"}

    entry.is_cancelled = True
    if entry.task and not entry.task.done():
        # Task still running — CancelledError handler will close stt/tts
        entry.task.cancel()
    elif entry.is_ready:
        # Task already finished — open connections must be closed directly
        asyncio.create_task(_close_prewarm_services(entry.stt, entry.tts))
    logger.debug(f"[prewarm] call_sid={call_sid}: cancelled via DELETE")
    return {"status": "cancelled"}


@app.get("/health")
async def health_check():
    state_snapshot = await worker_state.get_health_snapshot()
    return {
        "status": "healthy",
        "available": state_snapshot["available"],
        "current_call": state_snapshot["current_call"],
    }


@app.post("/cancel")
async def cancel_current_call(_: None = Depends(verify_worker_auth)):
    if worker_state.active_task:
        try:
            await worker_state.active_task.cancel()
            return {"status": "cancelled"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "no_active_call"}
