"""Unit tests for RedisStateBackend using fakeredis."""
import time

import pytest
import pytest_asyncio
import fakeredis.aioredis

from app.services.worker_pool.redis_state import (
    RedisStateBackend,
    _LUA_FIND_AND_ASSIGN,
    _LUA_RELEASE_ASSIGNMENT,
    _LUA_REASSIGN,
)


@pytest_asyncio.fixture
async def backend():
    """RedisStateBackend wired to an in-process fakeredis instance."""
    b = RedisStateBackend.__new__(RedisStateBackend)
    b._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return b


# ---------------------------------------------------------------------------
# find_and_assign
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_and_assign_claims_first_free(backend):
    worker_id = await backend.find_and_assign(["w1", "w2"], "call-1", ttl=100)
    assert worker_id == "w1"

    state = await backend._redis.hgetall("invorto:worker:state:w1")
    assert state["current_call_sid"] == "call-1"
    assert float(state["assigned_at"]) == pytest.approx(time.time(), abs=5)

    call_owner = await backend._redis.get("invorto:worker:call:call-1")
    assert call_owner == "w1"


@pytest.mark.asyncio
async def test_find_and_assign_skips_busy_picks_next(backend):
    # Pre-assign w1
    await backend._redis.hset("invorto:worker:state:w1", "current_call_sid", "existing-call")

    worker_id = await backend.find_and_assign(["w1", "w2"], "call-2", ttl=100)
    assert worker_id == "w2"

    state = await backend._redis.hgetall("invorto:worker:state:w2")
    assert state["current_call_sid"] == "call-2"


@pytest.mark.asyncio
async def test_find_and_assign_returns_none_when_all_busy(backend):
    await backend._redis.hset("invorto:worker:state:w1", "current_call_sid", "c1")
    await backend._redis.hset("invorto:worker:state:w2", "current_call_sid", "c2")

    result = await backend.find_and_assign(["w1", "w2"], "call-3", ttl=100)
    assert result is None


@pytest.mark.asyncio
async def test_find_and_assign_empty_candidates(backend):
    result = await backend.find_and_assign([], "call-x", ttl=100)
    assert result is None


@pytest.mark.asyncio
async def test_find_and_assign_sets_ttl_on_hash(backend):
    await backend.find_and_assign(["w1"], "call-1", ttl=3600)
    ttl = await backend._redis.ttl("invorto:worker:state:w1")
    assert ttl > 0


@pytest.mark.asyncio
async def test_find_and_assign_empty_string_treated_as_free(backend):
    # Worker state exists but current_call_sid is empty string (previously released)
    await backend._redis.hset(
        "invorto:worker:state:w1", mapping={"current_call_sid": "", "assigned_at": ""}
    )
    worker_id = await backend.find_and_assign(["w1"], "call-new", ttl=100)
    assert worker_id == "w1"


# ---------------------------------------------------------------------------
# release_assignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_assignment_clears_state_and_call_key(backend):
    await backend.find_and_assign(["w1"], "call-1", ttl=100)

    await backend.release_assignment("w1", "call-1")

    state = await backend._redis.hgetall("invorto:worker:state:w1")
    assert "current_call_sid" not in state

    call_key = await backend._redis.get("invorto:worker:call:call-1")
    assert call_key is None


@pytest.mark.asyncio
async def test_release_assignment_idempotent(backend):
    await backend.release_assignment("w-nonexistent", "call-nonexistent")
    # Should not raise


# ---------------------------------------------------------------------------
# clear_worker_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_worker_state(backend):
    await backend.find_and_assign(["w1"], "call-1", ttl=100)
    await backend.clear_worker_state("w1")

    state = await backend._redis.hgetall("invorto:worker:state:w1")
    assert "current_call_sid" not in state


# ---------------------------------------------------------------------------
# reassign
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reassign_swaps_call_sid(backend):
    await backend.find_and_assign(["w1"], "old-sid", ttl=100)

    await backend.reassign("old-sid", "new-sid", "w1", ttl=100)

    old_key = await backend._redis.get("invorto:worker:call:old-sid")
    assert old_key is None

    new_key = await backend._redis.get("invorto:worker:call:new-sid")
    assert new_key == "w1"

    state = await backend._redis.hgetall("invorto:worker:state:w1")
    assert state["current_call_sid"] == "new-sid"


# ---------------------------------------------------------------------------
# get_worker_for_call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_worker_for_call_returns_worker_id(backend):
    await backend.find_and_assign(["w1"], "call-1", ttl=100)

    worker_id = await backend.get_worker_for_call("call-1")
    assert worker_id == "w1"


@pytest.mark.asyncio
async def test_get_worker_for_call_returns_none_when_absent(backend):
    result = await backend.get_worker_for_call("nonexistent-call")
    assert result is None


# ---------------------------------------------------------------------------
# get_worker_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_worker_state_returns_hash(backend):
    await backend.find_and_assign(["w1"], "call-1", ttl=100)

    state = await backend.get_worker_state("w1")
    assert state["current_call_sid"] == "call-1"


@pytest.mark.asyncio
async def test_get_worker_state_returns_empty_for_free_worker(backend):
    state = await backend.get_worker_state("w-unknown")
    assert state == {}


# ---------------------------------------------------------------------------
# batch_get_states
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_get_states_returns_all_workers(backend):
    await backend.find_and_assign(["w1", "w2"], "call-1", ttl=100)
    await backend.find_and_assign(["w2"], "call-2", ttl=100)

    states = await backend.batch_get_states(["w1", "w2", "w3"])
    assert states["w1"]["current_call_sid"] == "call-1"
    assert states["w2"]["current_call_sid"] == "call-2"
    assert states["w3"] == {}


@pytest.mark.asyncio
async def test_batch_get_states_empty_list(backend):
    result = await backend.batch_get_states([])
    assert result == {}


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_second_attempt(backend):
    call_count = 0

    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ConnectionError("transient")
        return "ok"

    result = await backend._with_retry(flaky, "test-op", retries=3, base_delay=0)
    assert result == "ok"
    assert call_count == 2


@pytest.mark.asyncio
async def test_with_retry_raises_after_exhaustion(backend):
    async def always_fail():
        raise ConnectionError("permanent")

    with pytest.raises(ConnectionError):
        await backend._with_retry(always_fail, "test-op", retries=2, base_delay=0)


# ---------------------------------------------------------------------------
# Atomicity: concurrent assignments cannot double-book
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_assignments_claim_different_workers(backend):
    import asyncio

    results = await asyncio.gather(
        backend.find_and_assign(["w1", "w2"], "call-A", ttl=100),
        backend.find_and_assign(["w1", "w2"], "call-B", ttl=100),
    )
    # Both should succeed and claim different workers
    assert set(results) == {"w1", "w2"}
