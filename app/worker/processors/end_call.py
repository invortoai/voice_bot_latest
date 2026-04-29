import asyncio
from typing import Optional

from loguru import logger
from pipecat.frames.frames import EndFrame, LLMFullResponseEndFrame, TextFrame
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class EndCallProcessor(FrameProcessor):
    """Monitors LLM text output for configured end-call phrases.

    Sits between the LLM and TTS in the pipeline. All frames are passed
    through immediately — no latency added to normal conversation flow.

    When a phrase is matched in the completed LLM response, a background
    task waits for an estimated TTS speaking duration then queues EndFrame,
    allowing the bot to finish saying goodbye before the call ends.
    """

    def __init__(self, end_phrases: list, **kwargs):
        super().__init__(**kwargs)
        self._phrases = [
            p.lower().strip() for p in (end_phrases or []) if p and p.strip()
        ]
        self._buffer = ""
        self._task: Optional[PipelineTask] = None

    def set_task(self, task: PipelineTask) -> None:
        self._task = task

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if self._phrases and direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TextFrame):
                self._buffer += frame.text
            elif isinstance(frame, LLMFullResponseEndFrame):
                buf = self._buffer.lower()
                self._buffer = ""
                for phrase in self._phrases:
                    if phrase in buf:
                        logger.info(
                            f"End-call phrase matched: {phrase!r} — scheduling hangup"
                        )
                        asyncio.create_task(self._end_after_tts(buf))
                        break

        await self.push_frame(frame, direction)

    async def _end_after_tts(self, response_text: str) -> None:
        # Estimate TTS speaking time: ~15 chars/sec + 1.5s for TTS latency + 0.5s buffer
        estimated_secs = max(3.0, len(response_text) / 15 + 2.0)
        logger.info(
            f"Waiting {estimated_secs:.1f}s for TTS to finish before ending call"
        )
        await asyncio.sleep(estimated_secs)
        if self._task:
            logger.info("Queuing EndFrame after end-call phrase")
            await self._task.queue_frames([EndFrame()])
