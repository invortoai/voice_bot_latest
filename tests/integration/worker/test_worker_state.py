"""Integration tests for WorkerPool state management (assignment, release)."""

import pytest

from app.services.worker_pool import WorkerStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pool():
    """Return a fresh LocalWorkerPool with two workers."""
    from app.services.worker_pool import LocalWorkerPool

    p = LocalWorkerPool(["w1:8765", "w2:8765"])
    # Manually populate workers (discover_workers() is async and needs event loop)
    p.workers = {
        "w1:8765": WorkerStatus(host="w1:8765", instance_id="w1:8765"),
        "w2:8765": WorkerStatus(host="w2:8765", instance_id="w2:8765"),
    }
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWorkerAssignment:
    async def test_get_and_assign_worker_returns_worker(self, pool):
        worker = await pool.get_and_assign_worker("CALL-001")
        assert worker is not None
        assert worker.current_call_sid == "CALL-001"
        assert worker.is_available is False

    async def test_get_and_assign_worker_is_atomic(self, pool):
        w1 = await pool.get_and_assign_worker("CALL-001")
        w2 = await pool.get_and_assign_worker("CALL-002")
        # Both calls should get different workers
        assert w1 is not None
        assert w2 is not None
        assert w1.instance_id != w2.instance_id

    async def test_no_worker_available_returns_none(self, pool):
        # Assign all workers
        await pool.get_and_assign_worker("CALL-001")
        await pool.get_and_assign_worker("CALL-002")

        worker = await pool.get_and_assign_worker("CALL-003")
        assert worker is None

    async def test_get_worker_for_call_finds_assigned(self, pool):
        await pool.get_and_assign_worker("CALL-XYZ")
        worker = await pool.get_worker_for_call("CALL-XYZ")
        assert worker is not None
        assert worker.current_call_sid == "CALL-XYZ"

    async def test_get_worker_for_call_unknown_returns_none(self, pool):
        worker = await pool.get_worker_for_call("UNKNOWN-CALL")
        assert worker is None


class TestWorkerRelease:
    async def test_release_makes_worker_available(self, pool):
        await pool.get_and_assign_worker("CALL-001")
        await pool.release_worker("CALL-001")

        # After release, worker should be available for new assignment
        w = await pool.get_and_assign_worker("CALL-NEW")
        assert w is not None

    async def test_release_unknown_call_does_not_raise(self, pool):
        await pool.release_worker("NONEXISTENT-CALL")  # Should not raise

    async def test_release_clears_call_sid(self, pool):
        w = await pool.get_and_assign_worker("CALL-001")
        instance_id = w.instance_id
        await pool.release_worker("CALL-001")

        released_worker = pool.workers[instance_id]
        assert released_worker.current_call_sid is None
        assert released_worker.is_available is True


class TestWorkerReassign:
    async def test_reassign_updates_call_sid(self, pool):
        await pool.get_and_assign_worker("TEMP-ID")
        await pool.reassign_call_sid("TEMP-ID", "REAL-CALL-SID")

        worker = await pool.get_worker_for_call("REAL-CALL-SID")
        assert worker is not None

    async def test_reassign_old_sid_no_longer_tracked(self, pool):
        await pool.get_and_assign_worker("TEMP-ID")
        await pool.reassign_call_sid("TEMP-ID", "REAL-CALL-SID")

        old_worker = await pool.get_worker_for_call("TEMP-ID")
        assert old_worker is None


class TestWorkerHealthDemotion:
    async def test_unhealthy_worker_not_assigned(self, pool):
        # Mark one worker as unhealthy (3+ consecutive failures)
        w1 = pool.workers["w1:8765"]
        w1.consecutive_failures = 3

        # Only w2 should be available
        worker = await pool.get_and_assign_worker("CALL-001")
        assert worker is not None
        assert worker.instance_id == "w2:8765"

    async def test_all_unhealthy_returns_none(self, pool):
        for w in pool.workers.values():
            w.consecutive_failures = 5

        worker = await pool.get_and_assign_worker("CALL-001")
        assert worker is None


class TestWorkerStatus:
    def test_ws_url_with_public_ws_url(self):
        from unittest.mock import patch

        with patch("app.services.worker_pool.base.PUBLIC_WS_URL", "wss://example.com"):
            w = WorkerStatus(host="localhost:8765")
            url = w.get_ws_url("/ws/mcube/CALL-001")
        assert url == "wss://example.com/ws/mcube/CALL-001"

    def test_ws_url_without_public_ws_url_uses_host(self):
        from unittest.mock import patch

        with (
            patch("app.services.worker_pool.base.PUBLIC_WS_URL", ""),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_SCHEME", "wss"),
        ):
            w = WorkerStatus(host="localhost:8765")
            url = w.get_ws_url("/ws/mcube/CALL-001")
        assert "localhost:8765" in url
        assert "/ws/mcube/CALL-001" in url

    def test_to_dict_has_expected_keys(self):
        w = WorkerStatus(host="localhost:8765", instance_id="inst-1")
        d = w.to_dict()
        assert "host" in d
        assert "instance_id" in d
        assert "is_available" in d
        assert "current_call_sid" in d
