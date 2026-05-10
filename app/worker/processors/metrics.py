import logging

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    MetricsFrame,
    TTSStartedFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.worker.call_events import emit_turn_completed
from app.worker.metrics import CallMetrics
from app.worker.otel_metrics import (
    record_llm_ttfb,
    record_service_error,
    record_stt_ttfb,
    record_tts_ttfb,
    record_turn_latency,
)
from app.observability.utils import safe_observe

_log = logging.getLogger(__name__)

# Map Pipecat processor name substrings → (service_label, gen_ai_system).
# Checked in order; first match wins. If a processor name doesn't match any
# entry the observation is recorded under service="unknown" — a Pipecat rename
# becomes a visible anomaly in the dashboard rather than silently missing data.
_TTFB_ROUTING: list[tuple[str, str, str]] = [
    ("deepgram", "stt", "deepgram"),
    ("openai", "llm", "openai"),
    ("elevenlabs", "tts", "elevenlabs"),
    ("stt", "stt", "unknown"),
    ("llm", "llm", "unknown"),
    ("tts", "tts", "unknown"),
]

_TTFB_RECORDERS = {
    "stt": record_stt_ttfb,
    "llm": record_llm_ttfb,
    "tts": record_tts_ttfb,
}


class MetricsProcessor(FrameProcessor):
    """Pipecat FrameProcessor that feeds observed frames into CallMetrics and
    emits real-time OTEL metric observations.

    Positioned at the END of the pipeline (after assistant_aggregator) so it
    receives all downstream frames. All frames pass through unchanged.
    Audio frames (the high-frequency majority) are fast-pathed past the
    isinstance checks to keep per-frame overhead minimal.
    """

    def __init__(
        self,
        call_metrics: CallMetrics,
        org_id: str = "unknown",
        provider: str = "unknown",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._m = call_metrics
        self._org_id = org_id
        self._provider = provider
        self._turn_index = 0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if not isinstance(
            frame,
            (
                UserStartedSpeakingFrame,
                UserStoppedSpeakingFrame,
                BotStartedSpeakingFrame,
                BotStoppedSpeakingFrame,
                MetricsFrame,
                TTSStartedFrame,
            ),
        ):
            await self.push_frame(frame, direction)
            return

        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, UserStartedSpeakingFrame):
                self._m.on_user_started_speaking()
            elif isinstance(frame, UserStoppedSpeakingFrame):
                self._m.on_user_stopped_speaking()
            elif isinstance(frame, TTSStartedFrame):
                self._m.on_tts_started()
            elif isinstance(frame, BotStartedSpeakingFrame):
                turn = self._m.on_bot_started_speaking()
                if turn is not None:
                    safe_observe(
                        record_turn_latency,
                        self._org_id,
                        self._provider,
                        turn["total_ms"],
                    )
                    safe_observe(
                        emit_turn_completed,
                        self._turn_index,
                        turn,
                        self._org_id,
                        self._provider,
                    )
                    self._turn_index += 1
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self._m.on_bot_stopped_speaking()
            elif isinstance(frame, MetricsFrame):
                self._m.on_metrics_frame(frame)
                self._emit_otel_ttfb(frame)

        await self.push_frame(frame, direction)

    def _emit_otel_ttfb(self, frame: MetricsFrame) -> None:
        """Emit one OTEL histogram observation per TTFB in the MetricsFrame.

        Each observation is an in-memory append — no per-turn network call.
        Processor routing uses _TTFB_ROUTING so a Pipecat class rename produces
        a visible "unknown" observation rather than silently dropping data.
        """
        for item in frame.data:
            if not isinstance(item, TTFBMetricsData):
                continue

            value_ms = item.value * 1000
            p = item.processor.lower()
            model = item.model or "unknown"

            service, system = "unknown", "unknown"
            for token, svc, sys_ in _TTFB_ROUTING:
                if token in p:
                    service, system = svc, sys_
                    break

            if service == "unknown":
                _log.debug(
                    "TTFB from unrecognised processor %r — recording as unknown",
                    item.processor,
                )

            recorder = _TTFB_RECORDERS.get(service)
            if recorder:
                safe_observe(
                    recorder,
                    model=model,
                    system=system,
                    org_id=self._org_id,
                    value_ms=value_ms,
                )


class TranscriptionStatsProcessor(FrameProcessor):
    """Lightweight processor positioned between STT and user_aggregator.

    TranscriptionFrame is consumed by user_aggregator and never reaches
    MetricsProcessor at the end of the pipeline, so this processor captures
    transcript stats before user_aggregator consumes the frame.
    """

    def __init__(
        self,
        call_metrics: CallMetrics,
        org_id: str = "unknown",
        stt_system: str = "deepgram",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._m = call_metrics
        self._org_id = org_id
        self._stt_system = stt_system

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if (
            isinstance(frame, TranscriptionFrame)
            and direction == FrameDirection.DOWNSTREAM
        ):
            self._m.on_transcript(frame.text)
            if not frame.text.strip():
                safe_observe(
                    record_service_error,
                    "stt",
                    self._stt_system,
                    "EmptyTranscript",
                    self._org_id,
                )
        await self.push_frame(frame, direction)
