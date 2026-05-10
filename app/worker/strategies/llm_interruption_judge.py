import asyncio
import contextlib
import time
from typing import Optional

from loguru import logger
from openai import AsyncOpenAI
from opentelemetry import trace

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.turns.user_start.base_user_turn_start_strategy import (
    BaseUserTurnStartStrategy,
)
from pipecat.utils.asyncio.task_manager import BaseTaskManager

try:
    from pipecat.utils.tracing.turn_context_provider import get_current_turn_context
    from pipecat.utils.tracing.conversation_context_provider import (
        ConversationContextProvider,
    )

    _TRACING_AVAILABLE = True
except ImportError:  # pragma: no cover — pipecat without turn tracing
    _TRACING_AVAILABLE = False

    def get_current_turn_context():  # type: ignore[no-redef]
        return None

    ConversationContextProvider = None  # type: ignore[assignment]

from app.config import (
    LLM_JUDGE_MODEL,
    LLM_JUDGE_SYSTEM_PROMPT,
    LLM_JUDGE_TIMEOUT,
    LLM_JUDGE_INSTANT_INTERRUPT_WORD_COUNT,
)
from app.worker.pipecat_tracing import is_pipecat_tracing_enabled


def _capture_tracing_parent_context():
    """Best-effort parent OTel context for judge spans.

    Prefer the active turn's context so judge spans nest directly under a
    `turn`. When pipecat has closed the turn (e.g. SmartTurn ended the user
    turn before the bot finished speaking), fall back to the conversation
    context so spans at least nest under the call's root `conversation`
    span. Returns None if tracing is off or nothing is available — caller
    should then skip span creation.
    """
    if not _TRACING_AVAILABLE:
        return None
    try:
        ctx = get_current_turn_context()
        if ctx is not None:
            return ctx
        if ConversationContextProvider is not None:
            conv = ConversationContextProvider.get_instance()
            return conv.get_current_conversation_context()
    except Exception:
        return None
    return None


_TRACER = trace.get_tracer("invorto.interruption_judge")

_JUDGE_SYSTEM_PROMPT = LLM_JUDGE_SYSTEM_PROMPT


class LLMInterruptionJudgeStrategy(BaseUserTurnStartStrategy):
    """Classifies user speech during bot speaking as BACKCHANNEL or INTERRUPT.

    When the bot is NOT speaking, any user speech triggers a turn start immediately
    (zero added latency). When the bot IS speaking:

    - Tier 1: word count >= threshold -> instant INTERRUPT, no LLM call
    - Tier 2: word count < threshold, final transcript -> background LLM judge call;
      bot keeps speaking while the judge runs
    """

    def __init__(
        self,
        *,
        openai_api_key: str,
        model: str = LLM_JUDGE_MODEL,
        judge_timeout: float = LLM_JUDGE_TIMEOUT,
        instant_interrupt_word_count: int = LLM_JUDGE_INSTANT_INTERRUPT_WORD_COUNT,
        **kwargs,
    ):
        super().__init__(
            enable_interruptions=True,
            # Must be True so the aggregator broadcasts UserStartedSpeakingFrame
            # when we trigger a turn. Pipecat's TurnTrackingObserver depends on
            # that frame to create per-turn spans; with False, only the first
            # turn appears in Langfuse. The flag has no effect on backchannel
            # handling — the aggregator only broadcasts when we explicitly call
            # trigger_user_turn_started(), which we only do on Mode 1 and
            # tier1/INTERRUPT decisions.
            enable_user_speaking_frames=True,
            **kwargs,
        )
        self._openai_client = AsyncOpenAI(api_key=openai_api_key)
        self._model = model
        self._judge_timeout = judge_timeout
        self._instant_interrupt_word_count = instant_interrupt_word_count

        self._bot_speaking = False
        self._greeting_completed = False
        self._judge_sequence = 0
        self._pending_judge_task: Optional[asyncio.Task] = None
        self._call_metrics = None

    def set_call_metrics(self, metrics) -> None:
        """Attach CallMetrics for recording judge latency."""
        self._call_metrics = metrics

    async def setup(self, task_manager: BaseTaskManager):
        await super().setup(task_manager)
        logger.info("LLM interruption judge: initialized")

    async def reset(self):
        await super().reset()
        self._bot_speaking = False
        self._judge_sequence += 1
        if self._pending_judge_task and not self._pending_judge_task.done():
            self._pending_judge_task.cancel()
            self._pending_judge_task = None

    async def cleanup(self):
        await super().cleanup()
        self._judge_sequence += 1
        if self._pending_judge_task and not self._pending_judge_task.done():
            self._pending_judge_task.cancel()
            self._pending_judge_task = None
        await self._openai_client.close()

    async def process_frame(self, frame: Frame):
        await super().process_frame(frame)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            return

        if isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            if not self._greeting_completed:
                self._greeting_completed = True
                logger.debug("LLM judge: greeting completed")
            # Note: we do NOT cancel pending judges here. BotStoppedSpeakingFrame
            # fires on any 0.35s audio queue silence (including between TTS chunks),
            # so it's not a reliable "bot turn ended" signal. Let the judge complete
            # and act on its actual BACKCHANNEL/INTERRUPT decision.
            return

        is_final = isinstance(frame, TranscriptionFrame)
        is_interim = isinstance(frame, InterimTranscriptionFrame)
        if not is_final and not is_interim:
            return

        text = frame.text.strip()
        if not text:
            return

        word_count = len(text.split())
        needs_judge = self._bot_speaking or not self._greeting_completed

        # Mode 1: bot not speaking and greeting done -> immediate turn start
        if not needs_judge:
            await self.trigger_user_turn_started()
            return

        # Mode 2: bot speaking or greeting not done — apply tiered logic

        # Tier 1: long utterance -> instant interrupt (interim or final)
        if word_count >= self._instant_interrupt_word_count:
            logger.info(
                f"LLM judge: tier1 instant interrupt, words={word_count}, text={text!r}"
            )
            await self.trigger_user_turn_started()
            return

        # For tier 2, only use final transcriptions
        if not is_final:
            return

        # Tier 2: short final transcript -> background LLM judge
        self._judge_sequence += 1
        seq = self._judge_sequence
        if self._pending_judge_task and not self._pending_judge_task.done():
            self._pending_judge_task.cancel()

        bot_status = "greeting_phase" if not self._greeting_completed else "speaking"

        # Capture a parent OTel context synchronously so the background judge
        # task can attach its span to the active trace in Langfuse. Must happen
        # BEFORE create_task() — asyncio task boundaries don't propagate OTel
        # context automatically. Prefer turn context; fall back to conversation
        # context when pipecat has already closed the turn (e.g. between turns
        # while the bot's TTS is playing).
        parent_context = (
            _capture_tracing_parent_context() if is_pipecat_tracing_enabled() else None
        )

        self._pending_judge_task = self.task_manager.create_task(
            self._run_judge(text, bot_status, seq, parent_context),
            f"llm-judge-{seq}",
        )
        logger.info(
            f"LLM judge: tier2 fired, seq={seq}, words={word_count}, text={text!r}"
        )

    async def _run_judge(
        self,
        text: str,
        bot_status: str,
        seq: int,
        parent_context=None,
    ):
        # If tracing is on, open a child span.  Prefer the captured parent
        # (turn or conversation context) so the judge nests under the active
        # call trace in Langfuse.  If no parent context was captured, open a
        # standalone span anyway — it becomes a new root trace in Langfuse
        # (less ideal visually, but never silently dropped).
        if is_pipecat_tracing_enabled():
            if parent_context is not None:
                span_cm = _TRACER.start_as_current_span(
                    "interruption_judge", context=parent_context
                )
            else:
                span_cm = _TRACER.start_as_current_span("interruption_judge")
        else:
            span_cm = contextlib.nullcontext()

        try:
            with span_cm as span:
                system_msg = _JUDGE_SYSTEM_PROMPT.format(
                    bot_status=bot_status,
                    stt_output=text,
                )

                if span is not None:
                    span.set_attribute("gen_ai.system", "openai")
                    span.set_attribute("gen_ai.request.model", self._model)
                    span.set_attribute("gen_ai.request.temperature", 0)
                    span.set_attribute("gen_ai.request.max_tokens", 3)
                    span.set_attribute("input.text", text[:500])
                    span.set_attribute("input.bot_status", bot_status)
                    span.set_attribute("input.system_prompt", system_msg[:4000])
                    span.set_attribute("judge.sequence", seq)
                    span.set_attribute("judge.timeout_secs", self._judge_timeout)

                t0 = time.monotonic()
                response = await asyncio.wait_for(
                    self._openai_client.chat.completions.create(
                        model=self._model,
                        messages=[
                            {"role": "user", "content": system_msg},
                        ],
                        max_completion_tokens=3,
                        temperature=0,
                        service_tier="priority",
                    ),
                    timeout=self._judge_timeout,
                )
                judge_latency_ms = (time.monotonic() - t0) * 1000
                if self._call_metrics is not None:
                    self._call_metrics.on_interrupt_llm_latency(judge_latency_ms)

                if seq != self._judge_sequence:
                    if span is not None:
                        span.set_attribute("judge.result", "STALE")
                    logger.debug(
                        f"LLM judge: stale result discarded "
                        f"(seq={seq}, current={self._judge_sequence})"
                    )
                    return

                raw = (response.choices[0].message.content or "").strip()
                result = raw.upper()
                classification = (
                    "BACKCHANNEL" if result.startswith("BACK") else "INTERRUPT"
                )
                fingerprint = getattr(response, "system_fingerprint", None)

                if span is not None:
                    span.set_attribute("output.classification", classification)
                    span.set_attribute("output.raw", raw[:100])
                    span.set_attribute("judge.latency_ms", round(judge_latency_ms))
                    if fingerprint:
                        span.set_attribute("openai.system_fingerprint", fingerprint)
                    usage = getattr(response, "usage", None)
                    if usage is not None:
                        prompt_tokens = getattr(usage, "prompt_tokens", None)
                        completion_tokens = getattr(usage, "completion_tokens", None)
                        if prompt_tokens is not None:
                            span.set_attribute(
                                "gen_ai.usage.input_tokens", prompt_tokens
                            )
                        if completion_tokens is not None:
                            span.set_attribute(
                                "gen_ai.usage.output_tokens", completion_tokens
                            )

                if classification == "BACKCHANNEL":
                    logger.info(
                        f"LLM judge: BACKCHANNEL, latency_ms={judge_latency_ms:.0f}, text={text!r}"
                    )
                    # Do nothing — bot keeps speaking
                else:
                    logger.info(
                        f"LLM judge: INTERRUPT (result={result!r}), "
                        f"latency_ms={judge_latency_ms:.0f}, text={text!r}"
                    )
                    await self.trigger_user_turn_started()

        except asyncio.CancelledError:
            logger.debug(f"LLM judge: cancelled (seq={seq})")
            raise
        except Exception as e:
            if seq != self._judge_sequence:
                return
            logger.warning(
                f"LLM judge: error ({type(e).__name__}: {e}), "
                f"defaulting to interrupt, text={text!r}"
            )
            await self.trigger_user_turn_started()
