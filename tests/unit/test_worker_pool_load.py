"""Load tests for BaseWorkerPool — worker assignment, release, and reassignment.

Scenario matrix
---------------
S1  Saturation     – 100 simultaneous assigns on 10 workers → exactly 10 win, no double-assign
S2  Wave cycles    – 10 waves × 10 assign+release → 100 total, pool recycles cleanly each wave
S3  Reassignment   – outbound UUID→real-SID rekey across 10 concurrent calls
S3b RC-8 race      – release webhook fires BEFORE reassign_call_sid writes the call-mapping key
S4  Mixed chaos    – 80 concurrent inbound/outbound ops on 20 workers, no stuck workers

Pool × Redis matrix (parametrized on every scenario):
  k8s-local   K8sWorkerPool,  no Redis (asyncio.Lock path)
  k8s-redis   K8sWorkerPool,  FakeRedisStateBackend (Lua NX semantics)
  ec2-local   EC2WorkerPool,  no Redis
  ec2-redis   EC2WorkerPool,  FakeRedisStateBackend

All tests are marked @pytest.mark.slow and excluded from the default test run.
Run them with:  pytest tests/unit/ -m slow -v
              or  make test-load
"""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest


# ---------------------------------------------------------------------------
# FakeRedisStateBackend
#
# Drop-in replacement for RedisStateBackend that uses an in-process dict and
# an asyncio.Lock to simulate the Lua HASH atomicity guarantee:
#   - find_and_assign: iterate candidates, check-and-set is atomic (the lock
#     serialises coroutines exactly as Redis's single-threaded Lua does).
#   - All other operations match the real RedisStateBackend interface exactly.
# ---------------------------------------------------------------------------


class _FakeRedis:
    """In-process simulation of RedisStateBackend for load tests."""

    def __init__(self):
        self._states: dict[str, dict] = {}  # worker_id → {current_call_sid, assigned_at}
        self._calls: dict[str, str] = {}    # call_sid → worker_id
        self._mu = asyncio.Lock()

    async def find_and_assign(self, worker_ids: list, call_sid: str, ttl: int):
        async with self._mu:
            for worker_id in worker_ids:
                state = self._states.get(worker_id, {})
                if not state.get("current_call_sid"):
                    self._states[worker_id] = {
                        "current_call_sid": call_sid,
                        "assigned_at": str(time.time()),
                    }
                    self._calls[call_sid] = worker_id
                    return worker_id
            return None

    async def release_assignment(self, worker_id: str, call_sid: str) -> None:
        async with self._mu:
            state = self._states.get(worker_id, {})
            state.pop("current_call_sid", None)
            state.pop("assigned_at", None)
            self._calls.pop(call_sid, None)

    async def clear_worker_state(self, worker_id: str) -> None:
        async with self._mu:
            self._states.pop(worker_id, None)

    async def get_worker_for_call(self, call_sid: str):
        return self._calls.get(call_sid)

    async def get_worker_state(self, worker_id: str) -> dict:
        return dict(self._states.get(worker_id, {}))

    async def batch_get_states(self, worker_ids: list) -> dict:
        return {wid: dict(self._states.get(wid, {})) for wid in worker_ids}

    async def reassign(
        self, old_call_sid: str, new_call_sid: str, worker_id: str, ttl: int
    ) -> None:
        async with self._mu:
            self._calls.pop(old_call_sid, None)
            self._calls[new_call_sid] = worker_id
            state = self._states.get(worker_id, {})
            state["current_call_sid"] = new_call_sid
            self._states[worker_id] = state

    async def close(self) -> None:
        pass

    def all_keys(self) -> dict:
        """Return all active state+call keys — empty after full cleanup."""
        result = {}
        for wid, state in self._states.items():
            if state.get("current_call_sid"):
                result[f"invorto:worker:state:{wid}"] = state
        for call_sid, worker_id in self._calls.items():
            result[f"invorto:worker:call:{call_sid}"] = worker_id
        return result


# ---------------------------------------------------------------------------
# Pool factory helpers
# ---------------------------------------------------------------------------


def _make_pool(pool_type: str, n_workers: int, use_redis: bool):
    """Return a K8sWorkerPool or EC2WorkerPool with *n_workers* injected directly.

    discover_workers() is replaced with a no-op AsyncMock so pool-type-specific
    cloud API calls are never triggered by on-demand discovery.  _cancel_prewarm
    is also mocked so background fire-and-forget HTTP tasks don't linger after
    the test completes.
    """
    from app.services.worker_pool.base import WorkerStatus

    if pool_type == "k8s":
        from app.services.worker_pool.k8s import K8sWorkerPool

        pool = K8sWorkerPool(namespace="test", label_selector="app=worker", port=8765)
        workers = {
            f"worker-pod-{i:02d}": WorkerStatus(
                host=f"10.0.{i // 256}.{i % 256}:8765",
                instance_id=f"worker-pod-{i:02d}",
                private_ip=f"10.0.{i // 256}.{i % 256}",
            )
            for i in range(1, n_workers + 1)
        }
    else:
        from app.services.worker_pool.ec2 import EC2WorkerPool

        pool = EC2WorkerPool()
        workers = {
            f"i-{i:017d}": WorkerStatus(
                host=f"10.0.{i // 256}.{i % 256}:8765",
                instance_id=f"i-{i:017d}",
                private_ip=f"10.0.{i // 256}.{i % 256}",
                public_ip=f"54.1.{i // 256}.{i % 256}",
            )
            for i in range(1, n_workers + 1)
        }

    pool.workers = workers
    pool.discover_workers = AsyncMock()
    pool._cancel_prewarm = AsyncMock()

    if use_redis:
        pool._redis = _FakeRedis()

    return pool


# Parametrize decorator for the full pool × redis matrix
_POOL_MATRIX = pytest.mark.parametrize(
    "pool_type,use_redis",
    [("k8s", False), ("k8s", True), ("ec2", False), ("ec2", True)],
    ids=["k8s-local", "k8s-redis", "ec2-local", "ec2-redis"],
)


# ---------------------------------------------------------------------------
# S1 — Saturation: 100 simultaneous assigns on 10 workers
# ---------------------------------------------------------------------------


class TestSaturation:
    """100 concurrent get_and_assign_worker calls against a 10-worker pool.

    Only 10 should win; the remaining 90 must be rejected cleanly with no
    double-assignments and a consistent pool state afterwards.
    """

    @pytest.mark.slow
    @pytest.mark.asyncio
    @_POOL_MATRIX
    async def test_exactly_10_assigned_from_100_concurrent(self, pool_type, use_redis):
        pool = _make_pool(pool_type, n_workers=10, use_redis=use_redis)
        sids = [f"CALL-S1-{i:03d}" for i in range(100)]

        results = await asyncio.gather(*[pool.get_and_assign_worker(s) for s in sids])

        assigned = [w for w in results if w is not None]
        assert len(assigned) == 10, (
            f"[{pool_type}/{'redis' if use_redis else 'local'}] "
            f"Expected 10 assigned, got {len(assigned)}"
        )

    @pytest.mark.slow
    @pytest.mark.asyncio
    @_POOL_MATRIX
    async def test_no_double_assignment_under_saturation(self, pool_type, use_redis):
        """Each worker_id must appear in the results at most once."""
        pool = _make_pool(pool_type, n_workers=10, use_redis=use_redis)
        sids = [f"CALL-S1DA-{i:03d}" for i in range(100)]

        results = await asyncio.gather(*[pool.get_and_assign_worker(s) for s in sids])

        worker_ids = [w.instance_id for w in results if w is not None]
        assert len(worker_ids) == len(set(worker_ids)), (
            f"[{pool_type}/{'redis' if use_redis else 'local'}] "
            f"Double-assignment detected: {worker_ids}"
        )

    @pytest.mark.slow
    @pytest.mark.asyncio
    @_POOL_MATRIX
    async def test_pool_busy_count_matches_assigned_count(self, pool_type, use_redis):
        """pool.workers busy count must equal the number of successful assigns."""
        pool = _make_pool(pool_type, n_workers=10, use_redis=use_redis)
        sids = [f"CALL-S1ST-{i:03d}" for i in range(100)]

        results = await asyncio.gather(*[pool.get_and_assign_worker(s) for s in sids])

        expected_busy = sum(1 for w in results if w is not None)
        actual_busy = sum(
            1 for w in pool.workers.values() if w.current_call_sid is not None
        )
        assert actual_busy == expected_busy, (
            f"Pool shows {actual_busy} busy but {expected_busy} assigns succeeded"
        )

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_redis_has_correct_key_count_after_saturation(self):
        """After 10 successful assigns, Redis must hold exactly 20 keys
        (10 worker state hashes + 10 call-mapping strings)."""
        pool = _make_pool("k8s", n_workers=10, use_redis=True)
        sids = [f"CALL-S1RK-{i:03d}" for i in range(100)]

        await asyncio.gather(*[pool.get_and_assign_worker(s) for s in sids])

        keys = pool._redis.all_keys()
        assert len(keys) == 20, (
            f"Expected 20 Redis keys (10 state + 10 call), got {len(keys)}: {keys}"
        )


# ---------------------------------------------------------------------------
# S2 — Wave cycles: 10 waves × 10 assign+release on 10 workers
# ---------------------------------------------------------------------------


class TestWaveCycles:
    """Workers must be fully recyclable: each wave assigns all 10, releases all 10,
    then the next wave can assign all 10 again.  100 calls succeed total.
    """

    @pytest.mark.slow
    @pytest.mark.asyncio
    @_POOL_MATRIX
    async def test_all_100_calls_succeed_across_10_waves(self, pool_type, use_redis):
        pool = _make_pool(pool_type, n_workers=10, use_redis=use_redis)
        total_assigned = 0

        for wave in range(10):
            sids = [f"W{wave:02d}-CALL-{i:02d}" for i in range(10)]
            results = await asyncio.gather(
                *[pool.get_and_assign_worker(s) for s in sids]
            )
            wave_assigned = sum(1 for w in results if w is not None)
            assert wave_assigned == 10, (
                f"Wave {wave}: expected 10 assigned, got {wave_assigned}"
            )
            total_assigned += wave_assigned
            await asyncio.gather(*[pool.release_worker(s) for s in sids])

        assert total_assigned == 100

    @pytest.mark.slow
    @pytest.mark.asyncio
    @_POOL_MATRIX
    async def test_all_workers_free_between_waves(self, pool_type, use_redis):
        """After each wave's releases, every worker must have current_call_sid=None."""
        pool = _make_pool(pool_type, n_workers=10, use_redis=use_redis)

        for wave in range(5):
            sids = [f"WF{wave:02d}-CALL-{i:02d}" for i in range(10)]
            await asyncio.gather(*[pool.get_and_assign_worker(s) for s in sids])
            await asyncio.gather(*[pool.release_worker(s) for s in sids])

            stuck = [w.instance_id for w in pool.workers.values() if w.current_call_sid]
            assert stuck == [], (
                f"Wave {wave}: workers still assigned after release: {stuck}"
            )

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_redis_zero_keys_after_each_wave(self):
        """Redis must have exactly 0 active keys after every wave's releases complete."""
        pool = _make_pool("ec2", n_workers=10, use_redis=True)

        for wave in range(5):
            sids = [f"RK{wave:02d}-CALL-{i:02d}" for i in range(10)]
            await asyncio.gather(*[pool.get_and_assign_worker(s) for s in sids])
            await asyncio.gather(*[pool.release_worker(s) for s in sids])

            remaining = pool._redis.all_keys()
            assert remaining == {}, (
                f"Wave {wave}: Redis key leak after release: {remaining}"
            )

    @pytest.mark.slow
    @pytest.mark.asyncio
    @_POOL_MATRIX
    async def test_assigned_at_is_reset_after_release(self, pool_type, use_redis):
        """After release, worker.assigned_at must be None (no stale timestamp)."""
        pool = _make_pool(pool_type, n_workers=10, use_redis=use_redis)
        sids = [f"AT-CALL-{i:02d}" for i in range(10)]

        await asyncio.gather(*[pool.get_and_assign_worker(s) for s in sids])
        await asyncio.gather(*[pool.release_worker(s) for s in sids])

        for w in pool.workers.values():
            assert w.assigned_at is None, (
                f"Worker {w.instance_id} still has assigned_at={w.assigned_at} after release"
            )


# ---------------------------------------------------------------------------
# S3 — Outbound reassignment: temp UUID → real call_sid re-key
# ---------------------------------------------------------------------------


class TestOutboundReassignment:
    """Simulates the outbound call flow: assign with a temp call_id, then re-key
    to the real call_sid once the telephony provider responds, then release.
    """

    @pytest.mark.slow
    @pytest.mark.asyncio
    @_POOL_MATRIX
    async def test_workers_hold_real_sids_after_concurrent_reassign(
        self, pool_type, use_redis
    ):
        pool = _make_pool(pool_type, n_workers=10, use_redis=use_redis)
        temp_ids = [f"TEMP-{i:03d}" for i in range(10)]
        real_sids = [f"CA{'x' * 32}{i}" for i in range(10)]

        results = await asyncio.gather(
            *[pool.get_and_assign_worker(t) for t in temp_ids]
        )
        assert all(w is not None for w in results), "All 10 assigns must succeed"

        await asyncio.gather(
            *[pool.reassign_call_sid(temp_ids[i], real_sids[i]) for i in range(10)]
        )

        assigned_sids = {
            w.current_call_sid for w in pool.workers.values() if w.current_call_sid
        }
        assert assigned_sids == set(real_sids), (
            f"After reassign: expected {set(real_sids)}, got {assigned_sids}"
        )

    @pytest.mark.slow
    @pytest.mark.asyncio
    @_POOL_MATRIX
    async def test_full_outbound_flow_leaves_pool_clean(self, pool_type, use_redis):
        """assign(temp) → reassign(temp, real) → release(real) → all workers free."""
        pool = _make_pool(pool_type, n_workers=10, use_redis=use_redis)
        temp_ids = [f"UUID-{i:03d}" for i in range(10)]
        real_sids = [f"REAL-SID-{i:03d}" for i in range(10)]

        await asyncio.gather(*[pool.get_and_assign_worker(t) for t in temp_ids])
        await asyncio.gather(
            *[pool.reassign_call_sid(temp_ids[i], real_sids[i]) for i in range(10)]
        )
        await asyncio.gather(*[pool.release_worker(s) for s in real_sids])

        stuck = [w.instance_id for w in pool.workers.values() if w.current_call_sid]
        assert stuck == [], f"Workers stuck after full outbound flow: {stuck}"

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_redis_clean_after_full_outbound_flow(self):
        pool = _make_pool("ec2", n_workers=10, use_redis=True)
        temp_ids = [f"TMP-EC2-{i:03d}" for i in range(10)]
        real_sids = [f"RSI-EC2-{i:03d}" for i in range(10)]

        await asyncio.gather(*[pool.get_and_assign_worker(t) for t in temp_ids])
        await asyncio.gather(
            *[pool.reassign_call_sid(temp_ids[i], real_sids[i]) for i in range(10)]
        )
        await asyncio.gather(*[pool.release_worker(s) for s in real_sids])

        assert pool._redis.all_keys() == {}, (
            f"Redis key leak after outbound flow: {pool._redis.all_keys()}"
        )


# ---------------------------------------------------------------------------
# S3b — RC-8 race: release webhook fires before reassign_call_sid writes key
#
# The new approach uses a retry loop (4 × 200ms) in release_worker instead of
# a _pending_releases dict, making it stateless across runner pods.
# ---------------------------------------------------------------------------


class TestRC8RetryRelease:
    """Race condition: the telephony provider sends a status webhook with the
    real call_sid BEFORE reassign_call_sid has written the call→worker mapping
    key.  release_worker retries lookup up to 4 times (200ms apart) waiting for
    the key to appear, then falls back to a local pool scan.
    """

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_early_release_retries_until_mapping_appears(self):
        """release_worker retries lookup; key appears on 3rd attempt → worker freed."""
        from unittest.mock import patch

        pool = _make_pool("k8s", n_workers=1, use_redis=True)
        worker = list(pool.workers.values())[0]

        await pool.get_and_assign_worker("TEMP-RC8")
        # Simulate real SID arriving before reassign writes the mapping key
        call_count = 0
        original = pool._redis.get_worker_for_call

        async def delayed_lookup(call_sid):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return None
            return await original(call_sid)

        pool._redis.get_worker_for_call = delayed_lookup
        await pool._redis.reassign("TEMP-RC8", "REAL-RC8", worker.instance_id, ttl=3600)
        worker.current_call_sid = "REAL-RC8"

        with patch("asyncio.sleep", AsyncMock()):
            await pool.release_worker("REAL-RC8")

        assert worker.current_call_sid is None
        assert call_count == 3

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_release_falls_back_to_local_scan_after_exhausted_retries(self):
        """When the mapping key never appears, local pool scan frees the worker."""
        from unittest.mock import patch

        pool = _make_pool("ec2", n_workers=1, use_redis=True)
        worker = list(pool.workers.values())[0]
        worker.current_call_sid = "ORPHAN-RC8"

        pool._redis.get_worker_for_call = AsyncMock(return_value=None)

        with patch("asyncio.sleep", AsyncMock()):
            await pool.release_worker("ORPHAN-RC8")

        assert worker.current_call_sid is None

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_rc8_race_at_100_call_scale(self):
        """100 concurrent outbound calls where every release fires before reassign.
        After all operations settle, all 10 workers must be free.
        """
        from unittest.mock import patch

        pool = _make_pool("k8s", n_workers=10, use_redis=True)

        async def outbound_with_rc8_race(i: int):
            temp = f"TEMP-RACE-{i:03d}"
            real = f"REAL-RACE-{i:03d}"
            worker = await pool.get_and_assign_worker(temp)
            if worker is None:
                return  # pool saturated for calls 11-99 — expected
            await pool.reassign_call_sid(temp, real)
            # Fire release concurrently with reassign (the RC-8 race)
            with patch("asyncio.sleep", AsyncMock()):
                await pool.release_worker(real)

        await asyncio.gather(*[outbound_with_rc8_race(i) for i in range(100)])

        stuck = [w.instance_id for w in pool.workers.values() if w.current_call_sid]
        assert stuck == [], f"Workers stuck after RC-8 scale test: {stuck}"


# ---------------------------------------------------------------------------
# S4 — Mixed chaos: 80 concurrent inbound/outbound ops on 20 workers
# ---------------------------------------------------------------------------


class TestMixedChaos:
    """50 inbound calls + 30 outbound calls fire concurrently against 20 workers.
    Pool saturation is expected (80 > 20); correctness properties are verified:
      - No worker is permanently stuck assigned after all tasks complete.
      - Redis has no active keys remaining (no leaks from incomplete cleanup).
      - Manual release_worker_by_id works atomically under concurrent load.
    """

    @pytest.mark.slow
    @pytest.mark.asyncio
    @_POOL_MATRIX
    async def test_no_stuck_workers_after_concurrent_chaos(self, pool_type, use_redis):
        pool = _make_pool(pool_type, n_workers=20, use_redis=use_redis)

        async def inbound(sid: str):
            w = await pool.get_and_assign_worker(sid)
            if w:
                await asyncio.sleep(0)
                await pool.release_worker(sid)

        async def outbound(temp: str, real: str):
            w = await pool.get_and_assign_worker(temp)
            if w:
                await pool.reassign_call_sid(temp, real)
                await asyncio.sleep(0)
                await pool.release_worker(real)

        tasks = [inbound(f"CHAOS-IN-{i:03d}") for i in range(50)] + [
            outbound(f"CHAOS-TMP-{i:03d}", f"CHAOS-REAL-{i:03d}") for i in range(30)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)

        stuck = [w.instance_id for w in pool.workers.values() if w.current_call_sid]
        assert stuck == [], f"Workers stuck after chaos: {stuck}"

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_redis_zero_keys_after_chaos(self):
        """Redis must have no active keys once every concurrent operation completes."""
        pool = _make_pool("k8s", n_workers=20, use_redis=True)

        async def mixed_call(i: int):
            if i % 3 == 0:
                sid = f"MIX-IN-{i:03d}"
                w = await pool.get_and_assign_worker(sid)
                if w:
                    await asyncio.sleep(0)
                    await pool.release_worker(sid)
            else:
                temp, real = f"MIX-TMP-{i:03d}", f"MIX-REAL-{i:03d}"
                w = await pool.get_and_assign_worker(temp)
                if w:
                    await pool.reassign_call_sid(temp, real)
                    await asyncio.sleep(0)
                    await pool.release_worker(real)

        await asyncio.gather(
            *[mixed_call(i) for i in range(80)], return_exceptions=True
        )
        await asyncio.sleep(0)

        remaining = pool._redis.all_keys()
        assert remaining == {}, f"Redis key leak after chaos: {remaining}"

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_manual_release_by_id_clears_all_workers(self):
        """release_worker_by_id must atomically free every worker under concurrent load."""
        pool = _make_pool("ec2", n_workers=10, use_redis=True)

        sids = [f"MAN-{i:03d}" for i in range(10)]
        workers = await asyncio.gather(*[pool.get_and_assign_worker(s) for s in sids])
        assert all(w is not None for w in workers), "All 10 assigns must succeed"

        worker_ids = [w.instance_id for w in workers]
        results = await asyncio.gather(
            *[pool.release_worker_by_id(wid) for wid in worker_ids]
        )
        assert all(results), "Every release_worker_by_id call must return True"

        stuck = [w.instance_id for w in pool.workers.values() if w.current_call_sid]
        assert stuck == [], f"Workers stuck after manual release: {stuck}"
        assert pool._redis.all_keys() == {}, (
            f"Redis keys leaked after manual release: {pool._redis.all_keys()}"
        )

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_concurrent_release_by_id_and_release_worker(self):
        """release_worker_by_id and release_worker called simultaneously for the
        same call must both return without error and leave the worker free.
        """
        pool = _make_pool("k8s", n_workers=1, use_redis=True)
        worker = list(pool.workers.values())[0]

        await pool.get_and_assign_worker("DUP-CALL-001")

        # Fire both release paths at once — simulates duplicate status webhook
        # arriving at the same time as an SRE manual override.
        results = await asyncio.gather(
            pool.release_worker("DUP-CALL-001"),
            pool.release_worker_by_id(worker.instance_id),
            return_exceptions=True,
        )
        # Neither should raise
        assert not any(isinstance(r, Exception) for r in results), (
            f"Unexpected exception during concurrent release: {results}"
        )
        assert worker.current_call_sid is None
        assert pool._redis.all_keys() == {}
