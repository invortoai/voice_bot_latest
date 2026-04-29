"""Integration tests for the /workers management endpoints.

Covers:
- GET /workers  — lists pool status and availability counts
- POST /workers/refresh — triggers worker discovery
- POST /workers/{id}/release — manually releases a worker
"""

import pytest

from app.services.worker_pool import WorkerStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def two_workers():
    """Add two workers to the global pool, clean up afterwards."""
    from app.services.worker_pool import worker_pool

    w1 = WorkerStatus(host="w1:8765", instance_id="test-worker-A")
    w2 = WorkerStatus(host="w2:8765", instance_id="test-worker-B")
    worker_pool.workers["test-worker-A"] = w1
    worker_pool.workers["test-worker-B"] = w2
    yield w1, w2
    # Cleanup
    worker_pool.workers.pop("test-worker-A", None)
    worker_pool.workers.pop("test-worker-B", None)


# ---------------------------------------------------------------------------
# GET /workers
# ---------------------------------------------------------------------------


class TestListWorkersEndpoint:
    async def test_empty_pool_returns_zero_counts(self, runner_client):
        """When no workers are registered, totals are zero."""
        from app.services.worker_pool import worker_pool

        # Preserve real pool state
        saved = dict(worker_pool.workers)
        worker_pool.workers.clear()
        try:
            resp = await runner_client.get("/workers")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 0
            assert data["available"] == 0
            assert data["workers"] == []
        finally:
            worker_pool.workers.update(saved)

    async def test_returns_all_registered_workers(self, runner_client, two_workers):
        resp = await runner_client.get("/workers")
        assert resp.status_code == 200
        data = resp.json()
        worker_ids = {w["worker_id"] for w in data["workers"]}
        assert "test-worker-A" in worker_ids
        assert "test-worker-B" in worker_ids

    async def test_total_count_matches_registered(self, runner_client, two_workers):
        resp = await runner_client.get("/workers")
        data = resp.json()
        assert data["total"] == 2

    async def test_available_count_when_all_free(self, runner_client, two_workers):
        resp = await runner_client.get("/workers")
        data = resp.json()
        assert data["available"] == 2

    async def test_available_count_decreases_when_worker_busy(
        self, runner_client, two_workers
    ):
        w1, w2 = two_workers
        w1.current_call_sid = "CA-BUSY-001"
        try:
            resp = await runner_client.get("/workers")
            data = resp.json()
            assert data["available"] == 1
        finally:
            w1.current_call_sid = None

    async def test_available_count_zero_when_all_busy(self, runner_client, two_workers):
        w1, w2 = two_workers
        w1.current_call_sid = "CA-BUSY-W1"
        w2.current_call_sid = "CA-BUSY-W2"
        try:
            resp = await runner_client.get("/workers")
            data = resp.json()
            assert data["available"] == 0
        finally:
            for w in (w1, w2):
                w.current_call_sid = None

    async def test_unhealthy_worker_not_counted_as_available(
        self, runner_client, two_workers
    ):
        w1, w2 = two_workers
        w1.consecutive_failures = 3  # demoted
        try:
            resp = await runner_client.get("/workers")
            data = resp.json()
            assert data["available"] == 1  # only w2 counts
        finally:
            w1.consecutive_failures = 0

    async def test_worker_dict_has_expected_keys(self, runner_client, two_workers):
        resp = await runner_client.get("/workers")
        worker = resp.json()["workers"][0]
        for key in ["worker_id", "is_available", "current_call_sid"]:
            assert key in worker, f"Missing key: {key}"
        # Infrastructure details should NOT be exposed
        for key in ["host", "private_ip", "public_ip"]:
            assert key not in worker, f"Sensitive key should be redacted: {key}"


# ---------------------------------------------------------------------------
# POST /workers/refresh
# ---------------------------------------------------------------------------


class TestRefreshWorkersEndpoint:
    async def test_refresh_returns_200(self, runner_client):
        resp = await runner_client.post("/workers/refresh")
        assert resp.status_code == 200

    async def test_refresh_returns_status_and_count(self, runner_client):
        resp = await runner_client.post("/workers/refresh")
        data = resp.json()
        assert data["status"] == "refreshed"
        assert "worker_count" in data


# ---------------------------------------------------------------------------
# POST /workers/{id}/release
# ---------------------------------------------------------------------------


class TestReleaseWorkerEndpoint:
    async def test_release_busy_worker_returns_ok(self, runner_client, two_workers):
        w1, _ = two_workers
        w1.current_call_sid = "CA-RELEASE"

        resp = await runner_client.post("/workers/test-worker-A/release")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "released"
        assert data["worker_id"] == "test-worker-A"

    async def test_release_restores_worker_availability(
        self, runner_client, two_workers
    ):
        w1, _ = two_workers
        w1.current_call_sid = "CA-RESTORE"

        await runner_client.post("/workers/test-worker-A/release")

        assert w1.is_accepting_calls is True
        assert w1.current_call_sid is None

    async def test_release_unknown_worker_returns_404(self, runner_client):
        resp = await runner_client.post("/workers/nonexistent-worker/release")
        assert resp.status_code == 404

    async def test_release_idle_worker_returns_ok(self, runner_client, two_workers):
        """Releasing an already-idle worker should still succeed."""
        resp = await runner_client.post("/workers/test-worker-A/release")
        assert resp.status_code == 200
