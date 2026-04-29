"""Stub out worker-only dependencies so unit tests run without the full
worker install (~2 GB of torch + pipecat).

These stubs are injected into sys.modules at conftest load time — before
any test module is imported — so that `app.worker.pipeline` can be imported
successfully in the CI environment that only has runner deps installed.
"""

import sys
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Concrete stub classes — real Python classes so isinstance() works correctly
# across both the production import and the test's import of the same module.
# ---------------------------------------------------------------------------


class _TextFrame:
    def __init__(self, text=""):
        self.text = text


class _EndFrame:
    pass


class _LLMFullResponseEndFrame:
    pass


class _TTSSpeakFrame:
    def __init__(self, text=""):
        self.text = text


class _OutputAudioRawFrame:
    pass


class _UserStartedSpeakingFrame:
    pass


class _UserStoppedSpeakingFrame:
    pass


class _BotStartedSpeakingFrame:
    pass


class _BotStoppedSpeakingFrame:
    pass


class _MetricsFrame:
    def __init__(self, data=None):
        self.data = data or []


# Stub metrics data classes
class _TTFBMetricsData:
    def __init__(self, processor="", value=0.0):
        self.processor = processor
        self.value = value


class _LLMUsageMetricsData:
    def __init__(self, processor="", value=None):
        self.processor = processor
        self.value = value


class _TTSUsageMetricsData:
    def __init__(self, processor="", value=0):
        self.processor = processor
        self.value = value


class _FrameDirection:
    DOWNSTREAM = "downstream"
    UPSTREAM = "upstream"


class _FrameProcessor:
    """Minimal base class mirroring pipecat's FrameProcessor interface."""

    def __init__(self, **kwargs):
        pass

    async def process_frame(self, frame, direction):
        pass

    async def push_frame(self, frame, direction=None):
        pass


# ---------------------------------------------------------------------------
# Helper: build a MagicMock module with explicit attribute overrides
# ---------------------------------------------------------------------------


def _make_mod(**attrs):
    m = MagicMock()
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


# ---------------------------------------------------------------------------
# Build stub modules for every pipecat sub-path that pipeline.py (and the
# modules it imports) reference at module level.
# ---------------------------------------------------------------------------

_frames_mod = _make_mod(
    TextFrame=_TextFrame,
    EndFrame=_EndFrame,
    LLMFullResponseEndFrame=_LLMFullResponseEndFrame,
    TTSSpeakFrame=_TTSSpeakFrame,
    OutputAudioRawFrame=_OutputAudioRawFrame,
    UserStartedSpeakingFrame=_UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame=_UserStoppedSpeakingFrame,
    BotStartedSpeakingFrame=_BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame=_BotStoppedSpeakingFrame,
    MetricsFrame=_MetricsFrame,
)

_metrics_mod = _make_mod(
    TTFBMetricsData=_TTFBMetricsData,
    LLMUsageMetricsData=_LLMUsageMetricsData,
    TTSUsageMetricsData=_TTSUsageMetricsData,
)

_frame_processor_mod = _make_mod(
    FrameProcessor=_FrameProcessor,
    FrameDirection=_FrameDirection,
)

_PIPECAT_STUBS: dict = {
    "pipecat": MagicMock(),
    "pipecat.frames": MagicMock(),
    "pipecat.frames.frames": _frames_mod,
    "pipecat.processors": MagicMock(),
    "pipecat.processors.frame_processor": _frame_processor_mod,
    "pipecat.processors.aggregators": MagicMock(),
    "pipecat.processors.aggregators.llm_response_universal": MagicMock(),
    "pipecat.audio": MagicMock(),
    "pipecat.audio.turn": MagicMock(),
    "pipecat.audio.turn.smart_turn": MagicMock(),
    "pipecat.audio.turn.smart_turn.base_smart_turn": MagicMock(),
    "pipecat.audio.turn.smart_turn.local_smart_turn_v3": MagicMock(),
    "pipecat.audio.vad": MagicMock(),
    "pipecat.audio.vad.silero": MagicMock(),
    "pipecat.audio.vad.vad_analyzer": MagicMock(),
    "pipecat.pipeline": MagicMock(),
    "pipecat.pipeline.pipeline": MagicMock(),
    "pipecat.pipeline.runner": MagicMock(),
    "pipecat.pipeline.task": MagicMock(),
    "pipecat.services": MagicMock(),
    "pipecat.services.openai": MagicMock(),
    "pipecat.services.openai.base_llm": MagicMock(),
    "pipecat.services.openai.llm": MagicMock(),
    "pipecat.services.deepgram": MagicMock(),
    "pipecat.services.deepgram.stt": MagicMock(),
    "pipecat.services.elevenlabs": MagicMock(),
    "pipecat.services.elevenlabs.tts": MagicMock(),
    "pipecat.services.stt_service": MagicMock(),
    "pipecat.turns": MagicMock(),
    "pipecat.turns.user_stop": MagicMock(),
    "pipecat.turns.user_turn_strategies": MagicMock(),
    "pipecat.utils": MagicMock(),
    "pipecat.utils.tracing": MagicMock(),
    "pipecat.utils.tracing.setup": MagicMock(),
    "pipecat.metrics": MagicMock(),
    "pipecat.metrics.metrics": _metrics_mod,
    # worker sub-packages — must be registered so app.worker.main is importable
    # in unit tests without the full worker install.
    # NOTE: app.worker.processors.* are real modules on disk — do NOT stub them.
    "app.worker.pipecat_tracing": MagicMock(),
    "app.worker.providers": MagicMock(),
    "app.worker.providers.base": MagicMock(),
    "app.worker.providers.jambonz": MagicMock(),
    "app.worker.providers.mcube": MagicMock(),
    "app.worker.providers.twilio": MagicMock(),
}

# deepgram — imported by app.worker.services for LiveOptions
_deepgram_stub = _make_mod(LiveOptions=MagicMock())

# aiohttp — imported at the top of app.worker.pipeline
_aiohttp_abc_stub = _make_mod(AbstractResolver=type("AbstractResolver", (), {}))
_aiohttp_stub = MagicMock()
_aiohttp_stub.abc = _aiohttp_abc_stub  # so aiohttp.abc.AbstractResolver is a real class

for _name, _mod in _PIPECAT_STUBS.items():
    sys.modules.setdefault(_name, _mod)

sys.modules.setdefault("deepgram", _deepgram_stub)
sys.modules.setdefault("aiohttp", _aiohttp_stub)
sys.modules.setdefault("aiohttp.abc", _aiohttp_abc_stub)


# ---------------------------------------------------------------------------
# Override root conftest fixtures that require a live Postgres container.
# Unit tests run without Docker / DB — these no-op overrides prevent the
# autouse clean_tables fixture from triggering pg_container setup.
# ---------------------------------------------------------------------------

import pytest as _pytest


@_pytest.fixture(scope="session")
def pg_container():
    """No-op override: unit tests don't need a Postgres testcontainer."""
    return None


@_pytest.fixture(scope="session")
def test_tenant():
    """No-op override: unit tests don't need a seeded test tenant."""
    return {}


@_pytest.fixture(autouse=True)
def clean_tables():
    """No-op override: unit tests don't touch the database."""
    yield
