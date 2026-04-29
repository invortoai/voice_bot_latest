import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

# Default audio params per telephony provider.
# Worker derives these from call_record["provider"] when prewarming.
_PROVIDER_DEFAULT_AUDIO_PARAMS: dict[str, Tuple[int, int, str]] = {
    "twilio": (8000, 8000, "mulaw"),
    "mcube": (8000, 8000, "mulaw"),
    "jambonz": (8000, 8000, "linear16"),
}

PREWARM_TTL_SECONDS = 60


@dataclass
class PrewarmEntry:
    call_sid: str
    created_at: float = field(default_factory=time.monotonic)
    # Audio params — set by _fill_prewarm once provider is known from DB
    in_rate: int = 8000
    out_rate: int = 8000
    encoding: str = "mulaw"
    # Cached org_id so _handle_call can attribute OTEL metrics correctly on a
    # full cache hit (when the DB fetch is skipped entirely).
    org_id: str = "unknown"
    # Configs — set by _fill_prewarm (from payload or DB)
    assistant_config: Optional[dict] = None
    phone_config: Optional[dict] = None
    custom_params: Optional[dict] = None
    # Services — set by _fill_prewarm after connecting
    stt: Optional[object] = None
    tts: Optional[object] = None
    vad_analyzer: Optional[object] = None
    smart_turn_analyzer: Optional[object] = None
    llm: Optional[object] = None
    end_call_processor: Optional[object] = None
    context: Optional[object] = None
    context_aggregator: Optional[object] = (
        None  # (user_aggregator, assistant_aggregator) tuple
    )
    prewarm_metrics: Optional[dict] = None  # timing breakdown from _fill_prewarm
    # Background task reference for awaiting / cancelling
    task: Optional[asyncio.Task] = None
    is_ready: bool = False
    is_cancelled: bool = False


class PrewarmCache:
    def __init__(self):
        self._entries: dict[str, PrewarmEntry] = {}
        self._lock = asyncio.Lock()

    async def put(self, entry: PrewarmEntry) -> None:
        async with self._lock:
            self._entries[entry.call_sid] = entry

    async def get(self, call_sid: str) -> Optional[PrewarmEntry]:
        async with self._lock:
            return self._entries.get(call_sid)

    async def remove(self, call_sid: str) -> Optional[PrewarmEntry]:
        async with self._lock:
            return self._entries.pop(call_sid, None)

    async def reassign(self, old_key: str, new_key: str) -> bool:
        """Atomically re-key an entry from old_key to new_key (call_id → call_sid)."""
        async with self._lock:
            entry = self._entries.pop(old_key, None)
            if entry is None:
                return False
            entry.call_sid = new_key
            self._entries[new_key] = entry
            return True

    async def evict_expired(self) -> list:
        """Remove and return entries older than PREWARM_TTL_SECONDS."""
        async with self._lock:
            now = time.monotonic()
            expired_sids = [
                call_sid
                for call_sid, entry in self._entries.items()
                if now - entry.created_at > PREWARM_TTL_SECONDS
            ]
            return [self._entries.pop(call_sid) for call_sid in expired_sids]


prewarm_cache = PrewarmCache()
