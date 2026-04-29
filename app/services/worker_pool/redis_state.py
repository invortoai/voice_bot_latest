import asyncio
import time
from typing import Optional

from loguru import logger

# Lua: atomically find the first available candidate worker and assign it.
# A worker is available when its current_call_sid HASH field is absent or empty.
#
# KEYS[1..N]:   invorto:worker:state:{worker_id}  for each candidate
# KEYS[N+1]:    invorto:worker:call:{call_sid}     reverse-mapping key
# ARGV[1]:      call_sid
# ARGV[2]:      assigned_at  (unix timestamp as string)
# ARGV[3]:      ttl          (seconds as string) — applied to both the HASH and the call key
# ARGV[4..N+3]: worker_ids   parallel to KEYS[1..N]
#
# Returns: the claimed worker_id, or false if all candidates were busy.
#
# NOTE: accesses keys across potentially different hash slots — not Redis-Cluster-safe.
# Standard (non-clustered) Redis only.
_LUA_FIND_AND_ASSIGN = """
local n = #KEYS - 1
for i = 1, n do
    local current = redis.call('HGET', KEYS[i], 'current_call_sid')
    if not current or current == '' then
        redis.call('HSET', KEYS[i], 'current_call_sid', ARGV[1], 'assigned_at', ARGV[2])
        redis.call('EXPIRE', KEYS[i], tonumber(ARGV[3]))
        redis.call('SET', KEYS[n + 1], ARGV[3 + i], 'EX', tonumber(ARGV[3]))
        return ARGV[3 + i]
    end
end
return false
"""

# Lua: atomically clear assignment HASH fields and delete the reverse-mapping key.
#
# KEYS[1]: invorto:worker:state:{worker_id}
# KEYS[2]: invorto:worker:call:{call_sid}
_LUA_RELEASE_ASSIGNMENT = """
redis.call('HDEL', KEYS[1], 'current_call_sid', 'assigned_at')
redis.call('DEL',  KEYS[2])
return 1
"""

# Lua: atomically reassign a worker from old_call_sid to new_call_sid.
#
# KEYS[1]: invorto:worker:call:{old_call_sid}
# KEYS[2]: invorto:worker:call:{new_call_sid}
# KEYS[3]: invorto:worker:state:{worker_id}
# ARGV[1]: worker_id
# ARGV[2]: new_call_sid
# ARGV[3]: ttl (seconds as string)
_LUA_REASSIGN = """
redis.call('DEL', KEYS[1])
redis.call('SET', KEYS[2], ARGV[1], 'EX', tonumber(ARGV[3]))
redis.call('HSET', KEYS[3], 'current_call_sid', ARGV[2])
return 1
"""


class RedisStateBackend:
    """Worker assignment state backend — single source of truth across all runner pods.

    Schema
    ------
    invorto:worker:state:{worker_id}  HASH  { current_call_sid, assigned_at }
        current_call_sid: empty string or absent  →  worker is free
        current_call_sid: non-empty string        →  worker is busy (value = call_sid)
        assigned_at: unix timestamp string when assigned, empty or absent when free
        TTL: set to _redis_key_ttl on assignment; safety net if release never fires

    invorto:worker:call:{call_sid}    STRING  worker_id  (reverse lookup for release)
        TTL: same as worker state HASH

    All multi-key mutations use Lua scripts so they are atomic on the Redis server —
    a runner crash mid-operation cannot leave half-applied state.

    Redis unavailability is handled by _with_retry() with linear backoff. All public
    methods raise on exhausted retries; callers decide whether to reject or degrade.
    """

    _KEY_WORKER_STATE = "invorto:worker:state:{worker_id}"
    _KEY_CALL = "invorto:worker:call:{call_sid}"

    def __init__(self, host: str, port: int) -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError as exc:
            raise RuntimeError(
                "redis[asyncio] is required for multi-runner deployments. "
                "Install: pip install 'redis[asyncio]>=5.0.0'"
            ) from exc

        self._redis = aioredis.Redis(
            host=host,
            port=port,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            retry_on_timeout=True,
        )
        logger.info(f"RedisStateBackend connected to {host}:{port}")

    # ------------------------------------------------------------------
    # Retry helper
    # ------------------------------------------------------------------

    async def _with_retry(
        self,
        coro_factory,
        op_name: str,
        retries: int = 3,
        base_delay: float = 0.3,
    ):
        """Execute an async operation with linear-backoff retry on any exception.

        Raises the last exception if all attempts fail.
        """
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(1, retries + 1):
            try:
                return await coro_factory()
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    delay = base_delay * attempt
                    logger.warning(
                        f"Redis {op_name} failed (attempt {attempt}/{retries}): {exc}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)
        raise last_exc

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------

    async def find_and_assign(
        self, candidate_ids: list[str], call_sid: str, ttl: int
    ) -> Optional[str]:
        """Atomically find the first available candidate and assign it to call_sid.

        Iterates candidate state HASHes inside a single Lua script — the
        check-and-set is fully atomic: no two runners can claim the same worker
        even under concurrent load.

        Returns the claimed worker_id, or None if all candidates were busy.
        """
        if not candidate_ids:
            return None

        worker_state_keys = [
            self._KEY_WORKER_STATE.format(worker_id=wid) for wid in candidate_ids
        ]
        call_key = self._KEY_CALL.format(call_sid=call_sid)
        all_keys = worker_state_keys + [call_key]
        assigned_at = str(time.time())

        async def _run():
            result = await self._redis.eval(
                _LUA_FIND_AND_ASSIGN,
                len(all_keys),
                *all_keys,
                call_sid,
                assigned_at,
                str(ttl),
                *candidate_ids,
            )
            return result or None

        return await self._with_retry(_run, "find_and_assign")

    # ------------------------------------------------------------------
    # Release
    # ------------------------------------------------------------------

    async def release_assignment(self, worker_id: str, call_sid: str) -> None:
        """Atomically clear the worker HASH fields and delete the reverse-mapping key."""
        worker_key = self._KEY_WORKER_STATE.format(worker_id=worker_id)
        call_key = self._KEY_CALL.format(call_sid=call_sid)

        async def _run():
            await self._redis.eval(_LUA_RELEASE_ASSIGNMENT, 2, worker_key, call_key)

        await self._with_retry(_run, "release_assignment")

    async def clear_worker_state(self, worker_id: str) -> None:
        """Clear assignment fields for a worker with no active call (e.g. manual release)."""
        key = self._KEY_WORKER_STATE.format(worker_id=worker_id)

        async def _run():
            await self._redis.hdel(key, "current_call_sid", "assigned_at")

        await self._with_retry(_run, "clear_worker_state")

    # ------------------------------------------------------------------
    # Reassignment
    # ------------------------------------------------------------------

    async def reassign(
        self, old_call_sid: str, new_call_sid: str, worker_id: str, ttl: int
    ) -> None:
        """Atomically reassign a worker from old_call_sid to new_call_sid.

        Three operations in one Lua script:
          DEL  invorto:worker:call:{old_call_sid}
          SET  invorto:worker:call:{new_call_sid}  →  worker_id
          HSET invorto:worker:state:{worker_id}    current_call_sid = new_call_sid
        """
        old_call_key = self._KEY_CALL.format(call_sid=old_call_sid)
        new_call_key = self._KEY_CALL.format(call_sid=new_call_sid)
        worker_key = self._KEY_WORKER_STATE.format(worker_id=worker_id)

        async def _run():
            await self._redis.eval(
                _LUA_REASSIGN,
                3,
                old_call_key,
                new_call_key,
                worker_key,
                worker_id,
                new_call_sid,
                str(ttl),
            )

        await self._with_retry(_run, "reassign")

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    async def get_worker_for_call(self, call_sid: str) -> Optional[str]:
        """Return the worker_id currently assigned to call_sid, or None."""
        key = self._KEY_CALL.format(call_sid=call_sid)

        async def _run():
            return await self._redis.get(key)

        return await self._with_retry(_run, "get_worker_for_call")

    async def get_worker_state(self, worker_id: str) -> dict:
        """Return the full state HASH for a single worker (empty dict if free/absent)."""
        key = self._KEY_WORKER_STATE.format(worker_id=worker_id)

        async def _run():
            return await self._redis.hgetall(key) or {}

        return await self._with_retry(_run, "get_worker_state")

    async def batch_get_states(self, worker_ids: list[str]) -> dict[str, dict]:
        """Fetch state HASHes for multiple workers in a single pipeline round-trip.

        Returns {worker_id: {current_call_sid, assigned_at}} for each worker.
        Workers with no active call return an empty dict or a dict with empty values.
        """
        if not worker_ids:
            return {}

        async def _run():
            async with self._redis.pipeline(transaction=False) as pipe:
                for wid in worker_ids:
                    pipe.hgetall(self._KEY_WORKER_STATE.format(worker_id=wid))
                results = await pipe.execute()
            return {wid: (result or {}) for wid, result in zip(worker_ids, results)}

        return await self._with_retry(_run, "batch_get_states")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._redis.aclose()
