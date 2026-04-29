"""Unit tests for pipeline audio utilities and EndCallProcessor."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.worker.pipeline import is_audio_url
from app.worker.processors.end_call import EndCallProcessor


class TestIsAudioUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/greeting.mp3",
            "https://example.com/audio.wav",
            "https://example.com/sound.ogg",
            "https://example.com/track.m4a",
            "https://example.com/clip.aac",
            "https://example.com/lossless.flac",
            "https://example.com/stream.webm",
            "https://example.com/raw.pcm",
            "https://example.com/video.mp4",
            "https://cdn.example.com/audio.mp3?v=123&token=abc",  # with query params
            "http://example.com/audio.mp3",  # http also allowed
        ],
    )
    def test_audio_url_returns_true(self, url):
        assert is_audio_url(url) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Hello, how can I help you?",  # plain text
            "https://example.com/page.html",  # HTML page
            "https://example.com/image.png",  # image
            "https://example.com/document.pdf",  # PDF
            "",  # empty string
            "   ",  # whitespace only
            None,  # None value
            "ftp://example.com/audio.mp3",  # wrong scheme
            "audio.mp3",  # no scheme
        ],
    )
    def test_non_audio_returns_false(self, text):
        assert is_audio_url(text) is False


class TestEndCallProcessor:
    """Tests for EndCallProcessor frame monitoring."""

    def _make_processor(self, phrases=None):
        return EndCallProcessor(end_phrases=phrases or ["goodbye", "have a nice day"])

    def test_init_strips_and_lowercases_phrases(self):
        proc = EndCallProcessor(end_phrases=["  Goodbye  ", "BYE", ""])
        assert proc._phrases == ["goodbye", "bye"]

    def test_init_none_phrases(self):
        proc = EndCallProcessor(end_phrases=None)
        assert proc._phrases == []

    def test_init_empty_phrases(self):
        proc = EndCallProcessor(end_phrases=[])
        assert proc._phrases == []

    def test_set_task(self):
        proc = self._make_processor()
        mock_task = MagicMock()
        proc.set_task(mock_task)
        assert proc._task is mock_task

    @pytest.mark.asyncio
    async def test_end_after_tts_queues_end_frame(self):
        """_end_after_tts should queue EndFrame on the pipeline task."""
        from pipecat.frames.frames import EndFrame

        proc = self._make_processor()
        mock_task = AsyncMock()
        proc.set_task(mock_task)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await proc._end_after_tts("bye")

        mock_task.queue_frames.assert_called_once()
        frames_arg = mock_task.queue_frames.call_args[0][0]
        assert len(frames_arg) == 1
        assert isinstance(frames_arg[0], EndFrame)

    @pytest.mark.asyncio
    async def test_end_after_tts_waits_minimum_3_seconds(self):
        """Short response text should still wait at least 3 seconds."""
        proc = self._make_processor()
        mock_task = AsyncMock()
        proc.set_task(mock_task)

        wait_times = []

        async def capture_sleep(secs):
            wait_times.append(secs)

        with patch("asyncio.sleep", side_effect=capture_sleep):
            await proc._end_after_tts("ok")

        assert wait_times[0] >= 3.0

    @pytest.mark.asyncio
    async def test_end_after_tts_no_task_does_not_raise(self):
        """If no task is set, _end_after_tts should not raise."""
        proc = self._make_processor()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await proc._end_after_tts("goodbye")  # should not raise

    @pytest.mark.asyncio
    async def test_process_frame_accumulates_text(self):
        """TextFrames between LLMFullResponseEndFrames should be accumulated."""
        from pipecat.frames.frames import TextFrame
        from pipecat.processors.frame_processor import FrameDirection

        proc = self._make_processor(phrases=["goodbye"])

        # Patch push_frame to avoid pipeline dependency
        proc.push_frame = AsyncMock()

        frame1 = TextFrame(text="Good")
        frame2 = TextFrame(text="bye")

        await proc.process_frame(frame1, FrameDirection.DOWNSTREAM)
        await proc.process_frame(frame2, FrameDirection.DOWNSTREAM)

        assert proc._buffer == "Goodbye"

    @pytest.mark.asyncio
    async def test_process_frame_resets_buffer_on_end(self):
        """Buffer should be cleared after LLMFullResponseEndFrame."""
        from pipecat.frames.frames import TextFrame, LLMFullResponseEndFrame
        from pipecat.processors.frame_processor import FrameDirection

        proc = self._make_processor(phrases=["foo"])
        proc.push_frame = AsyncMock()

        await proc.process_frame(TextFrame(text="some text"), FrameDirection.DOWNSTREAM)
        assert proc._buffer != ""

        await proc.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
        assert proc._buffer == ""
