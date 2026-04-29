"""Unit tests for WorkerState lifecycle (app/worker/state.py).

WorkerState is a single-tenant state machine that tracks one call at a time.
The design contract:
  - start_call()       → marks worker busy
  - end_call()         → restores availability, clears call and task
  - get_health_snapshot() → lock-consistent read for health endpoint
"""

import pytest


def _make_state():
    """Return a fresh WorkerState (not the module-level singleton)."""
    from app.worker.state import WorkerState

    return WorkerState()


class TestWorkerStateInitial:
    def test_initial_is_available(self):
        state = _make_state()
        assert state.is_available is True

    def test_initial_no_call_sid(self):
        state = _make_state()
        assert state.current_call_sid is None

    def test_initial_no_active_task(self):
        state = _make_state()
        assert state.active_task is None


class TestStartCallTransition:
    @pytest.mark.asyncio
    async def test_start_call_sets_unavailable(self):
        state = _make_state()
        await state.start_call("CALL-001")
        assert state.is_available is False

    @pytest.mark.asyncio
    async def test_start_call_stores_call_sid(self):
        state = _make_state()
        await state.start_call("CALL-ABC")
        assert state.current_call_sid == "CALL-ABC"

    @pytest.mark.asyncio
    async def test_start_call_with_different_sids(self):
        """Each fresh WorkerState can be reused for any call SID."""
        state = _make_state()
        await state.start_call("CA123")
        assert state.current_call_sid == "CA123"

        # Reset manually (simulating end_call/restart cycle)
        await state.end_call()
        await state.start_call("CA456")
        assert state.current_call_sid == "CA456"


class TestEndCallTransition:
    @pytest.mark.asyncio
    async def test_end_call_restores_availability(self):
        state = _make_state()
        await state.start_call("CALL-001")
        await state.end_call()
        assert state.is_available is True

    @pytest.mark.asyncio
    async def test_end_call_clears_call_sid(self):
        state = _make_state()
        await state.start_call("CALL-001")
        await state.end_call()
        assert state.current_call_sid is None

    @pytest.mark.asyncio
    async def test_end_call_clears_active_task(self):
        from unittest.mock import MagicMock

        state = _make_state()
        await state.start_call("CALL-001")
        state.active_task = MagicMock()
        await state.end_call()
        assert state.active_task is None

    @pytest.mark.asyncio
    async def test_end_call_without_start_does_not_raise(self):
        """Calling end_call on a fresh (idle) state is harmless."""
        state = _make_state()
        await state.end_call()  # should not raise
        assert state.is_available is True


class TestGetHealthSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_when_idle(self):
        state = _make_state()
        snapshot = await state.get_health_snapshot()
        assert snapshot["available"] is True
        assert snapshot["current_call"] is None

    @pytest.mark.asyncio
    async def test_snapshot_during_active_call(self):
        state = _make_state()
        await state.start_call("CALL-ACTIVE")
        snapshot = await state.get_health_snapshot()
        assert snapshot["available"] is False
        assert snapshot["current_call"] == "CALL-ACTIVE"

    @pytest.mark.asyncio
    async def test_snapshot_after_call_ends(self):
        state = _make_state()
        await state.start_call("CALL-ACTIVE")
        await state.end_call()
        snapshot = await state.get_health_snapshot()
        assert snapshot["available"] is True
        assert snapshot["current_call"] is None

    @pytest.mark.asyncio
    async def test_snapshot_has_required_keys(self):
        state = _make_state()
        snapshot = await state.get_health_snapshot()
        assert "available" in snapshot
        assert "current_call" in snapshot


class TestConcurrentStateAccess:
    @pytest.mark.asyncio
    async def test_sequential_call_cycles_do_not_corrupt_state(self):
        """Multiple start/end cycles on the same WorkerState remain consistent."""
        state = _make_state()
        for i in range(10):
            await state.start_call(f"CALL-{i}")
            assert state.current_call_sid == f"CALL-{i}"
            assert state.is_available is False
            await state.end_call()
            assert state.is_available is True
            assert state.current_call_sid is None
