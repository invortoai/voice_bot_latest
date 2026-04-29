"""Unit tests for BaseWorkerPool using fakeredis and mocked HTTP."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
import pytest_asyncio
import respx
import httpx

from app.services.worker_pool.base import BaseWorkerPool, WorkerStatus
from app.services.worker_pool.redis_state import RedisStateBackend


# ---------------------------------------------------------------------------
# Concrete pool subclass for testing
# ---------------------------------------------------------------------------


class _TestPool(BaseWorkerPool):
    async def discover_workers(self) -> None:
        pass


def _make_pool(use_redis: bool = True) -> tuple[_TestPool, RedisStateBackend | None]:
    pool = _TestPool()
    if use_redis:
        backend = RedisStateBackend.__new__(RedisStateBackend)
        backend._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        pool._redis = backend
        return pool, backend
    return pool, None


def _add_worker(pool: _TestPool, worker_id: str, private_ip: str = "10.0.0.1") -> WorkerStatus:
    w = WorkerStatus(
        host=f"{private_ip}:8765",
        instance_id=worker_id,
        private_ip=private_ip,
    )
    pool.workers[worker_id] = w
    return w


# ---------------------------------------------------------------------------
# Assignment — Redis path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_worker_redis_success():
    pool, _ = _make_pool()
    _add_worker(pool, "w1")

    worker = await pool.get_and_assign_worker("call-1")

    assert worker is not None
    assert worker.instance_id == "w1"
    assert worker.current_call_sid == "call-1"
    assert worker.assigned_at is not None


@pytest.mark.asyncio
async def test_assign_worker_redis_skips_busy():
    pool, backend = _make_pool()
    w1 = _add_worker(pool, "w1")
    w2 = _add_worker(pool, "w2")

    # Pre-assign w1 in Redis
    await backend.find_and_assign(["w1"], "existing-call", ttl=100)
    w1.current_call_sid = "existing-call"

    worker = await pool.get_and_assign_worker("call-new")
    assert worker is not None
    assert worker.instance_id == "w2"


@pytest.mark.asyncio
async def test_assign_worker_redis_returns_none_when_all_busy():
    pool, backend = _make_pool()
    w1 = _add_worker(pool, "w1")
    w1.current_call_sid = "busy"

    result = await pool.get_and_assign_worker("call-x")
    assert result is None


@pytest.mark.asyncio
async def test_assign_worker_redis_rejects_on_exhausted_retries():
    pool, backend = _make_pool()
    _add_worker(pool, "w1")

    backend.find_and_assign = AsyncMock(side_effect=ConnectionError("redis down"))

    with patch.object(pool, "_on_demand_discover", AsyncMock(return_value=False)):
        result = await pool.get_and_assign_worker("call-x")
    assert result is None


@pytest.mark.asyncio
async def test_assign_worker_redis_releases_orphan_when_worker_removed_from_pool():
    """Lua claims a worker that discover_workers concurrently removed → orphan is released."""
    pool, backend = _make_pool()
    _add_worker(pool, "w1")

    # Simulate: Lua claims w1, but by the time we check self.workers, it's gone
    original_find = backend.find_and_assign

    async def claim_then_evict(candidate_ids, call_sid, ttl):
        result = await original_find(candidate_ids, call_sid, ttl)
        # Simulate concurrent discover_workers removing w1
        pool.workers.pop("w1", None)
        return result

    backend.find_and_assign = claim_then_evict

    result = await pool.get_and_assign_worker("call-orphan")
    assert result is None

    # Redis orphan must be cleaned up
    await asyncio.sleep(0)  # let create_task fire
    assert await backend.get_worker_for_call("call-orphan") is None
    state = await backend.get_worker_state("w1")
    assert not state.get("current_call_sid")


# ---------------------------------------------------------------------------
# Assignment — local path (no Redis)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_worker_local_success():
    pool, _ = _make_pool(use_redis=False)
    _add_worker(pool, "w1")

    worker = await pool.get_and_assign_worker("call-1")
    assert worker is not None
    assert worker.current_call_sid == "call-1"


@pytest.mark.asyncio
async def test_assign_worker_local_skips_unhealthy():
    pool, _ = _make_pool(use_redis=False)
    w1 = _add_worker(pool, "w1")
    w1.consecutive_failures = 3  # unhealthy
    _add_worker(pool, "w2", "10.0.0.2")

    worker = await pool.get_and_assign_worker("call-1")
    assert worker is not None
    assert worker.instance_id == "w2"


# ---------------------------------------------------------------------------
# Release — normal path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_worker_clears_state():
    pool, backend = _make_pool()
    w1 = _add_worker(pool, "w1")

    assigned = await pool.get_and_assign_worker("call-1")
    assert assigned is not None

    with patch.object(pool, "_cancel_prewarm", AsyncMock()):
        await pool.release_worker("call-1")

    assert w1.current_call_sid is None
    assert w1.assigned_at is None

    # Redis should also be clean
    state = await backend.get_worker_state("w1")
    assert not state.get("current_call_sid")
    assert await backend.get_worker_for_call("call-1") is None


@pytest.mark.asyncio
async def test_release_worker_local_path():
    pool, _ = _make_pool(use_redis=False)
    w1 = _add_worker(pool, "w1")
    w1.current_call_sid = "call-1"
    w1.assigned_at = time.time()

    with patch.object(pool, "_cancel_prewarm", AsyncMock()):
        await pool.release_worker("call-1")

    assert w1.current_call_sid is None


# ---------------------------------------------------------------------------
# Release — RC-8 race: status webhook before reassign_call_sid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_worker_rc8_race_resolves_via_retry():
    """release_worker retries lookup; call mapping appears on 3rd attempt."""
    pool, backend = _make_pool()
    w1 = _add_worker(pool, "w1")
    w1.current_call_sid = "call-real"

    call_count = 0
    original = backend.get_worker_for_call

    async def delayed_lookup(call_sid):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return None  # Simulate key not yet written
        return await original(call_sid)

    backend.get_worker_for_call = delayed_lookup
    # Write the real mapping
    await backend._redis.set("invorto:worker:call:call-real", "w1")
    await backend._redis.hset("invorto:worker:state:w1", "current_call_sid", "call-real")

    with patch.object(pool, "_cancel_prewarm", AsyncMock()):
        with patch("asyncio.sleep", AsyncMock()):  # speed up test
            await pool.release_worker("call-real")

    assert w1.current_call_sid is None
    assert call_count == 3


@pytest.mark.asyncio
async def test_release_worker_falls_back_to_local_scan_after_retries():
    """When Redis key is never found, falls back to local pool scan."""
    pool, backend = _make_pool()
    w1 = _add_worker(pool, "w1")
    w1.current_call_sid = "call-orphan"
    w1.assigned_at = time.time()

    backend.get_worker_for_call = AsyncMock(return_value=None)

    with patch.object(pool, "_cancel_prewarm", AsyncMock()):
        with patch("asyncio.sleep", AsyncMock()):
            await pool.release_worker("call-orphan")

    assert w1.current_call_sid is None


# ---------------------------------------------------------------------------
# Release by ID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_worker_by_id():
    pool, backend = _make_pool()
    w1 = _add_worker(pool, "w1")
    await pool.get_and_assign_worker("call-1")

    with patch.object(pool, "_cancel_prewarm", AsyncMock()):
        result = await pool.release_worker_by_id("w1")

    assert result is True
    assert w1.current_call_sid is None
    assert await backend.get_worker_for_call("call-1") is None


@pytest.mark.asyncio
async def test_release_worker_by_id_not_found():
    pool, _ = _make_pool()
    result = await pool.release_worker_by_id("nonexistent")
    assert result is False


# ---------------------------------------------------------------------------
# Reassign call SID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reassign_call_sid_redis():
    pool, backend = _make_pool()
    w1 = _add_worker(pool, "w1")
    await pool.get_and_assign_worker("temp-uuid")

    await pool.reassign_call_sid("temp-uuid", "real-sid")

    assert w1.current_call_sid == "real-sid"
    assert await backend.get_worker_for_call("temp-uuid") is None
    assert await backend.get_worker_for_call("real-sid") == "w1"
    state = await backend.get_worker_state("w1")
    assert state["current_call_sid"] == "real-sid"


@pytest.mark.asyncio
async def test_reassign_call_sid_local():
    pool, _ = _make_pool(use_redis=False)
    w1 = _add_worker(pool, "w1")
    w1.current_call_sid = "temp-uuid"

    await pool.reassign_call_sid("temp-uuid", "real-sid")

    assert w1.current_call_sid == "real-sid"


@pytest.mark.asyncio
async def test_reassign_call_sid_missing_logs_warning(caplog):
    pool, _ = _make_pool(use_redis=False)
    _add_worker(pool, "w1")

    await pool.reassign_call_sid("nonexistent", "new-sid")
    # Should not raise; just logs a warning


# ---------------------------------------------------------------------------
# Health check: stale assignment release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_releases_stale_assignment(respx_mock):
    pool, backend = _make_pool()
    w1 = _add_worker(pool, "w1")
    w1.current_call_sid = "old-call"
    w1.assigned_at = time.time() - 200  # beyond grace

    respx_mock.get("http://10.0.0.1:8765/health").mock(
        return_value=httpx.Response(200, json={"current_call": None})
    )

    await pool.health_check_worker(w1)

    assert w1.current_call_sid is None


@pytest.mark.asyncio
async def test_health_check_respects_startup_grace(respx_mock):
    pool, _ = _make_pool()
    w1 = _add_worker(pool, "w1")
    w1.current_call_sid = "new-call"
    w1.assigned_at = time.time() - 10  # well within grace

    respx_mock.get("http://10.0.0.1:8765/health").mock(
        return_value=httpx.Response(200, json={"current_call": None})
    )

    await pool.health_check_worker(w1)

    # Should NOT release during grace period
    assert w1.current_call_sid == "new-call"


@pytest.mark.asyncio
async def test_health_check_increments_failures_on_error(respx_mock):
    pool, _ = _make_pool()
    w1 = _add_worker(pool, "w1")

    respx_mock.get("http://10.0.0.1:8765/health").mock(
        return_value=httpx.Response(500)
    )

    await pool.health_check_worker(w1)
    assert w1.consecutive_failures == 1


@pytest.mark.asyncio
async def test_health_check_resets_failures_on_recovery(respx_mock):
    pool, _ = _make_pool()
    w1 = _add_worker(pool, "w1")
    w1.consecutive_failures = 2

    respx_mock.get("http://10.0.0.1:8765/health").mock(
        return_value=httpx.Response(200, json={"current_call": None})
    )

    await pool.health_check_worker(w1)
    assert w1.consecutive_failures == 0


# ---------------------------------------------------------------------------
# get_all_workers_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_all_workers_state_returns_redis_data():
    pool, backend = _make_pool()
    _add_worker(pool, "w1")
    _add_worker(pool, "w2", "10.0.0.2")

    await pool.get_and_assign_worker("call-1")

    states = await pool.get_all_workers_state()
    by_id = {s["worker_id"]: s for s in states}

    assert by_id["w1"]["current_call_sid"] == "call-1"
    assert by_id["w1"]["is_available"] is False
    assert by_id["w2"]["current_call_sid"] is None
    assert by_id["w2"]["is_available"] is True


@pytest.mark.asyncio
async def test_get_all_workers_state_falls_back_to_local_on_redis_error():
    pool, backend = _make_pool()
    w1 = _add_worker(pool, "w1")
    w1.current_call_sid = "local-call"

    backend.batch_get_states = AsyncMock(side_effect=ConnectionError("redis down"))

    states = await pool.get_all_workers_state()
    assert states[0]["current_call_sid"] == "local-call"


@pytest.mark.asyncio
async def test_get_all_workers_state_no_redis():
    pool, _ = _make_pool(use_redis=False)
    w1 = _add_worker(pool, "w1")
    w1.current_call_sid = "call-1"

    states = await pool.get_all_workers_state()
    assert states[0]["current_call_sid"] == "call-1"


# ---------------------------------------------------------------------------
# _has_active_call (drain safety)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_active_call_local_cache():
    pool, _ = _make_pool(use_redis=False)
    w1 = _add_worker(pool, "w1")
    w1.current_call_sid = "c1"

    assert await pool._has_active_call(w1) is True

    w1.current_call_sid = None
    assert await pool._has_active_call(w1) is False


@pytest.mark.asyncio
async def test_has_active_call_checks_redis_when_local_blank():
    pool, backend = _make_pool()
    w1 = _add_worker(pool, "w1")
    # Local blank (simulating post-restart)
    assert w1.current_call_sid is None

    # But Redis has an active assignment
    await backend.find_and_assign(["w1"], "redis-call", ttl=100)

    assert await pool._has_active_call(w1) is True


@pytest.mark.asyncio
async def test_has_active_call_redis_error_defaults_to_true():
    pool, backend = _make_pool()
    w1 = _add_worker(pool, "w1")
    backend.get_worker_state = AsyncMock(side_effect=ConnectionError("down"))

    assert await pool._has_active_call(w1) is True


# ---------------------------------------------------------------------------
# Stale assignment reaping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_stale_assignments_clears_when_redis_shows_free():
    pool, backend = _make_pool()
    w1 = _add_worker(pool, "w1")
    w1.current_call_sid = "stale-call"
    w1.assigned_at = time.time() - 10000  # very old

    # Redis has no assignment (key was cleaned up by another runner)
    # (nothing written to Redis)

    with patch("app.services.worker_pool.base.WORKER_STALE_ASSIGNMENT_SECONDS", 1):
        await pool._release_stale_assignments()

    assert w1.current_call_sid is None


@pytest.mark.asyncio
async def test_release_stale_assignments_keeps_when_redis_still_active():
    pool, backend = _make_pool()
    w1 = _add_worker(pool, "w1")
    w1.current_call_sid = "live-call"
    w1.assigned_at = time.time() - 10000

    # Redis agrees the call is active
    await backend.find_and_assign(["w1"], "live-call", ttl=86400)

    with patch("app.services.worker_pool.base.WORKER_STALE_ASSIGNMENT_SECONDS", 1):
        await pool._release_stale_assignments()

    assert w1.current_call_sid == "live-call"


# ---------------------------------------------------------------------------
# Concurrent assignment integrity (no double-booking)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_calls_claim_different_workers():
    pool, _ = _make_pool()
    _add_worker(pool, "w1", "10.0.0.1")
    _add_worker(pool, "w2", "10.0.0.2")

    results = await asyncio.gather(
        pool.get_and_assign_worker("call-A"),
        pool.get_and_assign_worker("call-B"),
    )

    assigned_ids = {w.instance_id for w in results if w}
    assert len(assigned_ids) == 2, "Both workers should be claimed by different calls"

    # Local state consistent
    sids = {w.instance_id: w.current_call_sid for w in pool.workers.values()}
    assert "call-A" in sids.values()
    assert "call-B" in sids.values()
