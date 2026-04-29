import time
from typing import Optional

from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    SmartTurnMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)

from app.config import IS_LOCAL


class CallMetrics:
    """Collects per-call performance metrics in-memory during a call.

    Instantiated in _handle_call(), passed to create_pipeline(), and
    persisted to calls.metrics as a fire-and-forget task after the call ends.
    All methods are synchronous and sub-microsecond — zero pipeline impact.
    """

    def __init__(
        self,
        ws_accepted_at: float,
        stt_provider: str = "deepgram",
        stt_model: str = "",
        stt_language: str = "en",
        llm_provider: str = "openai",
        llm_model: str = "",
        tts_provider: str = "elevenlabs",
        tts_model: str = "",
        tts_voice_id: str = "",
    ):
        self._ws_accepted_at = ws_accepted_at
        self._pipeline_ready_at: Optional[float] = None
        self._client_connected_at: Optional[float] = None
        self._first_bot_audio_at: Optional[float] = None
        self._client_disconnected_at: Optional[float] = None

        self._stt_provider = stt_provider
        self._stt_model = stt_model
        self._stt_language = stt_language
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._tts_provider = tts_provider
        self._tts_model = tts_model
        self._tts_voice_id = tts_voice_id

        self._turn_start: Optional[float] = None
        self._turn_latencies_ms: list[float] = []

        self._bot_speaking = False
        self._interruptions = 0
        self._interrupt_llm_latencies_ms: list[float] = []

        self._vad_triggers = 0

        # TTS TTFB: measured as TTSStartedFrame → BotStartedSpeakingFrame wall-clock delta.
        # Pipecat's built-in TTS TTFB (from _receive_messages background task) does not
        # propagate reliably; this direct timing approach is used as the primary measurement.
        self._tts_started_at: Optional[float] = None

        self._turn_in_progress: bool = False
        self._pending_turn_stt_ms: Optional[float] = None
        self._pending_turn_llm_ms: Optional[float] = None
        self._pending_turn_tts_ms: Optional[float] = None

        # SmartTurnMetricsData(is_complete=True) is a SystemFrame so it arrives
        # at MetricsProcessor before UserStoppedSpeakingFrame (regular frame).
        self._last_smart_turn_e2e_ms: Optional[float] = None
        self._pending_smart_turn_ms: Optional[float] = None

        self._turns: list[dict] = []

        self._llm_tokens_in = 0
        self._llm_tokens_out = 0
        self._tts_characters = 0

        self._transcript_count = 0
        self._empty_transcripts = 0
        self._transcript_lengths: list[int] = []

        self._ended_by: Optional[str] = None
        self._error_type: Optional[str] = None

        self._prewarm_stt: bool = False
        self._prewarm_tts: bool = False
        self._prewarm_metrics: Optional[dict] = None

        self._runner_webhook_ms: Optional[float] = None
        self._transport_hop_ms: Optional[float] = None
        self._ws_msg_recv_at: Optional[float] = None
        self._config_resolve_at: Optional[float] = None
        self._greeting_queued_at: Optional[float] = None

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def record_pipeline_ready(self, t: float) -> None:
        self._pipeline_ready_at = t

    def record_client_connected(self) -> None:
        self._client_connected_at = time.monotonic()

    def record_client_disconnected(self) -> None:
        self._client_disconnected_at = time.monotonic()

    def record_call_ended(self, ended_by: str, error_type: Optional[str] = None) -> None:
        self._ended_by = ended_by
        self._error_type = error_type

    def record_prewarm_used(self, stt: bool, tts: bool) -> None:
        self._prewarm_stt = stt
        self._prewarm_tts = tts

    def set_prewarm_metrics(self, prewarm_metrics: dict) -> None:
        self._prewarm_metrics = prewarm_metrics

    def set_runner_webhook_ms(self, ms: float) -> None:
        self._runner_webhook_ms = ms

    def set_transport_hop_ms(self, ms: float) -> None:
        self._transport_hop_ms = ms

    def record_greeting_queued(self) -> None:
        self._greeting_queued_at = time.monotonic()

    # ------------------------------------------------------------------
    # Frame event hooks (called from MetricsProcessor / TranscriptionStatsProcessor)
    # ------------------------------------------------------------------

    def on_user_started_speaking(self) -> None:
        self._vad_triggers += 1
        if self._bot_speaking:
            self._interruptions += 1

    def on_tts_started(self) -> None:
        """Record when TTS synthesis begins.

        Only captures the FIRST TTSStartedFrame per bot turn — subsequent ones
        are ignored so multi-sentence turns report the first-sentence TTFB.
        """
        if self._tts_started_at is None:
            self._tts_started_at = time.monotonic()

    def on_user_stopped_speaking(self) -> None:
        self._turn_start = time.monotonic()
        self._turn_in_progress = True
        self._pending_turn_stt_ms = None
        self._pending_turn_llm_ms = None
        self._pending_turn_tts_ms = None
        # Reset so a stale value from a previous turn is never used.
        self._tts_started_at = None
        self._pending_smart_turn_ms = self._last_smart_turn_e2e_ms
        self._last_smart_turn_e2e_ms = None

    def on_bot_started_speaking(self) -> Optional[dict]:
        """Record bot-started-speaking event.

        Returns the completed turn dict (total_ms, stt/llm/tts_ttfb_ms,
        smart_turn_ms) if a turn was in progress, else None.
        """
        t = time.monotonic()
        self._bot_speaking = True
        if self._first_bot_audio_at is None:
            self._first_bot_audio_at = t

        tts_timing_ms: Optional[float] = None
        if self._tts_started_at is not None:
            tts_timing_ms = (t - self._tts_started_at) * 1000
            self._tts_started_at = None

        if self._turn_start is None:
            self._turn_in_progress = False
            return None

        total_ms = (t - self._turn_start) * 1000
        self._turn_latencies_ms.append(total_ms)

        # Prefer MetricsFrame-based tts_ms when available; fall back to timing delta.
        tts_ms = (
            self._pending_turn_tts_ms
            if self._pending_turn_tts_ms is not None
            else tts_timing_ms
        )
        turn = {
            "total_ms": round(total_ms, 1),
            "stt_ttfb_ms": round(self._pending_turn_stt_ms, 1)
            if self._pending_turn_stt_ms is not None
            else None,
            "llm_ttfb_ms": round(self._pending_turn_llm_ms, 1)
            if self._pending_turn_llm_ms is not None
            else None,
            "tts_ttfb_ms": round(tts_ms, 1) if tts_ms is not None else None,
            "smart_turn_ms": round(self._pending_smart_turn_ms, 1)
            if self._pending_smart_turn_ms is not None
            else None,
        }
        self._turns.append(turn)
        self._turn_start = None
        self._turn_in_progress = False
        return turn

    def on_bot_stopped_speaking(self) -> None:
        self._bot_speaking = False

    def on_interrupt_llm_latency(self, latency_ms: float) -> None:
        self._interrupt_llm_latencies_ms.append(latency_ms)

    def on_transcript(self, text: str) -> None:
        """Called from TranscriptionStatsProcessor (positioned before user_aggregator)."""
        self._transcript_count += 1
        stripped = text.strip()
        if not stripped:
            self._empty_transcripts += 1
        else:
            self._transcript_lengths.append(len(stripped))

    def on_metrics_frame(self, frame: MetricsFrame) -> None:
        try:
            for item in frame.data:
                p = item.processor.lower()
                if isinstance(item, TTFBMetricsData):
                    ms = item.value * 1000
                    if "deepgram" in p or "stt" in p:
                        if self._turn_in_progress and self._pending_turn_stt_ms is None:
                            self._pending_turn_stt_ms = ms
                    elif "openai" in p or "llm" in p:
                        if self._turn_in_progress and self._pending_turn_llm_ms is None:
                            self._pending_turn_llm_ms = ms
                    elif "elevenlabs" in p or "tts" in p:
                        if self._turn_in_progress and self._pending_turn_tts_ms is None:
                            self._pending_turn_tts_ms = ms
                elif isinstance(item, LLMUsageMetricsData):
                    self._llm_tokens_in += item.value.prompt_tokens
                    self._llm_tokens_out += item.value.completion_tokens
                elif isinstance(item, TTSUsageMetricsData):
                    self._tts_characters += item.value
                elif isinstance(item, SmartTurnMetricsData):
                    # SmartTurnMetricsData is a SystemFrame — arrives at MetricsProcessor
                    # before UserStoppedSpeakingFrame, so captured here first.
                    if item.is_complete:
                        self._last_smart_turn_e2e_ms = item.e2e_processing_time_ms
        except Exception as exc:
            if IS_LOCAL:
                raise
            import logging
            logging.getLogger(__name__).debug("on_metrics_frame error: %s", exc)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        def ms_diff(t1, t2):
            return round((t2 - t1) * 1000, 1) if t1 and t2 else None

        il: dict = {}
        if self._runner_webhook_ms is not None:
            il["runner_webhook_ms"] = round(self._runner_webhook_ms, 1)
        if self._transport_hop_ms is not None:
            il["transport_hop_ms"] = round(self._transport_hop_ms, 1)
        il["ws_msg_recv_ms"] = ms_diff(self._ws_accepted_at, self._ws_msg_recv_at)
        il["config_resolve_ms"] = ms_diff(self._ws_msg_recv_at, self._config_resolve_at)
        il["pipeline_build_ms"] = ms_diff(self._config_resolve_at, self._pipeline_ready_at)
        il["transport_start_ms"] = ms_diff(self._pipeline_ready_at, self._greeting_queued_at)
        il["greeting_tts_ttfb_ms"] = ms_diff(self._greeting_queued_at, self._first_bot_audio_at)
        worker_ms = ms_diff(self._ws_accepted_at, self._first_bot_audio_at)
        il["worker_ms"] = worker_ms
        if worker_ms is not None:
            il["total_ms"] = round(
                (self._runner_webhook_ms or 0) + (self._transport_hop_ms or 0) + worker_ms, 1
            )

        return {
            "prewarm": self._prewarm_metrics,
            "initial_latency": il if il.get("total_ms") is not None else None,
            "call_active_seconds": round(
                self._client_disconnected_at - self._client_connected_at, 1
            )
            if self._client_connected_at and self._client_disconnected_at
            else None,
            "ended_by": self._ended_by,
            "error_type": self._error_type,
            "prewarm_used": {"stt": self._prewarm_stt, "tts": self._prewarm_tts},
            "total_turns": len(self._turns),
            "interruptions": self._interruptions,
            "interrupt_llm_latency": {
                "avg_ms": _avg(self._interrupt_llm_latencies_ms),
                "p50_ms": _median(self._interrupt_llm_latencies_ms),
                "max_ms": round(max(self._interrupt_llm_latencies_ms), 1)
                if self._interrupt_llm_latencies_ms
                else None,
                "count": len(self._interrupt_llm_latencies_ms) or None,
            }
            if self._interrupt_llm_latencies_ms
            else None,
            "vad_triggers": self._vad_triggers,
            # Raw per-turn breakdown — consumers compute aggregates from this.
            # Coralogix DataPrime can unnest this array to compute p95 per org.
            "turns": self._turns,
            "stt": {
                "provider": self._stt_provider,
                "model": self._stt_model,
                "language": self._stt_language,
                "transcript_count": self._transcript_count or None,
                "empty_transcripts": self._empty_transcripts or None,
                "avg_transcript_length": round(
                    sum(self._transcript_lengths) / len(self._transcript_lengths), 1
                )
                if self._transcript_lengths
                else None,
            },
            "llm": {
                "provider": self._llm_provider,
                "model": self._llm_model,
                "tokens_in": self._llm_tokens_in or None,
                "tokens_out": self._llm_tokens_out or None,
            },
            "tts": {
                "provider": self._tts_provider,
                "model": self._tts_model,
                "voice_id": self._tts_voice_id,
                "characters": self._tts_characters or None,
            },
        }


def _avg(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return round(s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2, 1)
