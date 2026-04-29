"""Unit tests for K8sWorkerPool (app/services/worker_pool/k8s.py).

All kubernetes_asyncio calls are fully mocked — no cluster required.
kubernetes_asyncio need not be installed in the test environment.
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Stub kubernetes_asyncio before any app code imports it
# ---------------------------------------------------------------------------


class _ApiException(Exception):
    """Minimal stand-in for kubernetes_asyncio.client.exceptions.ApiException."""

    def __init__(self, status=0, reason=""):
        self.status = status
        self.reason = reason
        super().__init__(f"{status} {reason}")


if "kubernetes_asyncio" not in sys.modules:
    _mock_k8s_exceptions = MagicMock()
    _mock_k8s_exceptions.ApiException = _ApiException

    _mock_k8s_client = MagicMock()
    _mock_k8s_client.exceptions = _mock_k8s_exceptions

    _mock_k8s = MagicMock()
    _mock_k8s.client = _mock_k8s_client
    _mock_k8s.client.exceptions = _mock_k8s_exceptions
    _mock_k8s.config = MagicMock()

    sys.modules["kubernetes_asyncio"] = _mock_k8s
    sys.modules["kubernetes_asyncio.client"] = _mock_k8s_client
    sys.modules["kubernetes_asyncio.client.exceptions"] = _mock_k8s_exceptions
    sys.modules["kubernetes_asyncio.config"] = _mock_k8s.config
else:
    from kubernetes_asyncio.client.exceptions import ApiException as _ApiException  # type: ignore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pod(
    name: str,
    pod_ip: str,
    containers_ready: str = "True",
    deletion_timestamp=None,
    phase: str = "Running",
):
    """Build a minimal mock Kubernetes pod object."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.deletion_timestamp = deletion_timestamp
    pod.status.pod_ip = pod_ip
    pod.status.phase = phase

    ready_condition = MagicMock()
    ready_condition.type = "ContainersReady"
    ready_condition.status = containers_ready
    pod.status.conditions = [ready_condition]
    return pod


def _make_pool():
    from app.services.worker_pool.k8s import K8sWorkerPool

    return K8sWorkerPool(
        namespace="invorto",
        label_selector="app.kubernetes.io/name=invorto-worker",
        port=8765,
    )


def _mock_k8s_api(pods: list):
    """Return a mock v1 CoreV1Api whose list_namespaced_pod returns *pods*."""
    pod_list = MagicMock()
    pod_list.items = pods

    v1 = AsyncMock()
    v1.list_namespaced_pod = AsyncMock(return_value=pod_list)
    return v1


# ---------------------------------------------------------------------------
# discover_workers: happy path
# ---------------------------------------------------------------------------


class TestDiscoverWorkersHappyPath:
    @pytest.mark.asyncio
    async def test_ready_pod_is_added(self):
        pool = _make_pool()
        pod = _make_pod("worker-abc", "10.0.0.1")
        v1 = _mock_k8s_api([pod])
        pool._v1 = v1

        await pool.discover_workers()

        assert "worker-abc" in pool.workers
        assert pool.workers["worker-abc"].private_ip == "10.0.0.1"
        assert pool.workers["worker-abc"].host == "10.0.0.1:8765"

    @pytest.mark.asyncio
    async def test_multiple_ready_pods_all_added(self):
        pool = _make_pool()
        pods = [
            _make_pod("worker-1", "10.0.0.1"),
            _make_pod("worker-2", "10.0.0.2"),
        ]
        pool._v1 = _mock_k8s_api(pods)

        await pool.discover_workers()

        assert len(pool.workers) == 2
        assert "worker-1" in pool.workers
        assert "worker-2" in pool.workers

    @pytest.mark.asyncio
    async def test_public_ip_is_none_for_k8s_workers(self):
        pool = _make_pool()
        pool._v1 = _mock_k8s_api([_make_pod("worker-xyz", "10.0.1.5")])

        await pool.discover_workers()

        assert pool.workers["worker-xyz"].public_ip is None

    @pytest.mark.asyncio
    async def test_instance_id_equals_pod_name(self):
        pool = _make_pool()
        pool._v1 = _mock_k8s_api([_make_pod("worker-pod-42", "10.0.0.9")])

        await pool.discover_workers()

        assert pool.workers["worker-pod-42"].instance_id == "worker-pod-42"


# ---------------------------------------------------------------------------
# discover_workers: filtering
# ---------------------------------------------------------------------------


class TestDiscoverWorkersFiltering:
    @pytest.mark.asyncio
    async def test_pod_with_deletion_timestamp_excluded(self):
        pool = _make_pool()
        terminating = _make_pod(
            "worker-term", "10.0.0.5", deletion_timestamp="2024-01-01T00:00:00Z"
        )
        pool._v1 = _mock_k8s_api([terminating])

        await pool.discover_workers()

        assert "worker-term" not in pool.workers

    @pytest.mark.asyncio
    async def test_pod_with_containers_not_ready_excluded(self):
        pool = _make_pool()
        not_ready = _make_pod("worker-init", "10.0.0.6", containers_ready="False")
        pool._v1 = _mock_k8s_api([not_ready])

        await pool.discover_workers()

        assert "worker-init" not in pool.workers

    @pytest.mark.asyncio
    async def test_pod_with_no_ip_excluded(self):
        pool = _make_pool()
        no_ip = _make_pod("worker-no-ip", "")
        no_ip.status.pod_ip = None
        pool._v1 = _mock_k8s_api([no_ip])

        await pool.discover_workers()

        assert "worker-no-ip" not in pool.workers

    @pytest.mark.asyncio
    async def test_ready_and_unready_pods_mixed(self):
        pool = _make_pool()
        pods = [
            _make_pod("worker-good", "10.0.0.1"),
            _make_pod("worker-bad", "10.0.0.2", containers_ready="False"),
            _make_pod("worker-term", "10.0.0.3", deletion_timestamp="now"),
        ]
        pool._v1 = _mock_k8s_api(pods)

        await pool.discover_workers()

        assert "worker-good" in pool.workers
        assert "worker-bad" not in pool.workers
        assert "worker-term" not in pool.workers


# ---------------------------------------------------------------------------
# discover_workers: incremental updates
# ---------------------------------------------------------------------------


class TestDiscoverWorkersIncrementalUpdates:
    @pytest.mark.asyncio
    async def test_pod_ip_updated_when_rescheduled(self):
        pool = _make_pool()
        pool._v1 = _mock_k8s_api([_make_pod("worker-1", "10.0.0.1")])
        await pool.discover_workers()
        assert pool.workers["worker-1"].private_ip == "10.0.0.1"

        # Pod is rescheduled with a new IP
        pool._v1 = _mock_k8s_api([_make_pod("worker-1", "10.0.0.99")])
        await pool.discover_workers()

        assert pool.workers["worker-1"].private_ip == "10.0.0.99"
        assert pool.workers["worker-1"].host == "10.0.0.99:8765"

    @pytest.mark.asyncio
    async def test_disappeared_idle_pod_removed(self):
        pool = _make_pool()
        pool._v1 = _mock_k8s_api(
            [
                _make_pod("worker-1", "10.0.0.1"),
                _make_pod("worker-2", "10.0.0.2"),
            ]
        )
        await pool.discover_workers()
        assert len(pool.workers) == 2

        # worker-2 disappears (idle — no active call)
        pool._v1 = _mock_k8s_api([_make_pod("worker-1", "10.0.0.1")])
        await pool.discover_workers()

        assert "worker-1" in pool.workers
        assert "worker-2" not in pool.workers

    @pytest.mark.asyncio
    async def test_draining_pod_with_active_call_preserved(self):
        """A pod that disappears from K8s (deletionTimestamp set) but is still
        handling a call must remain in the pool until the call ends."""
        pool = _make_pool()
        pool._v1 = _mock_k8s_api(
            [
                _make_pod("worker-1", "10.0.0.1"),
                _make_pod("worker-2", "10.0.0.2"),
            ]
        )
        await pool.discover_workers()

        # worker-2 has an active call
        pool.workers["worker-2"].current_call_sid = "CALL-ACTIVE"

        # worker-2 disappears from K8s (pod is being drained)
        pool._v1 = _mock_k8s_api([_make_pod("worker-1", "10.0.0.1")])
        await pool.discover_workers()

        # worker-2 must still be in the pool so release_worker can find it
        assert "worker-2" in pool.workers
        assert pool.workers["worker-2"].current_call_sid == "CALL-ACTIVE"

    @pytest.mark.asyncio
    async def test_draining_pod_removed_after_call_ends(self):
        """Once a draining pod's call ends (current_call_sid cleared), the next
        discovery cycle must remove it."""
        pool = _make_pool()
        pool._v1 = _mock_k8s_api([_make_pod("worker-1", "10.0.0.1")])
        await pool.discover_workers()
        pool.workers["worker-1"].current_call_sid = "CALL-X"

        # Pod disappears — kept because call is active
        pool._v1 = _mock_k8s_api([])
        await pool.discover_workers()
        assert "worker-1" in pool.workers

        # Call ends — next cycle removes the pod
        pool.workers["worker-1"].current_call_sid = None
        await pool.discover_workers()
        assert "worker-1" not in pool.workers

    @pytest.mark.asyncio
    async def test_existing_assignment_state_preserved_on_rediscovery(self):
        pool = _make_pool()
        pool._v1 = _mock_k8s_api([_make_pod("worker-1", "10.0.0.1")])
        await pool.discover_workers()

        pool.workers["worker-1"].current_call_sid = "CALL-007"

        pool._v1 = _mock_k8s_api([_make_pod("worker-1", "10.0.0.1")])
        await pool.discover_workers()

        # Assignment state must not be wiped by rediscovery
        assert pool.workers["worker-1"].current_call_sid == "CALL-007"
        assert not pool.workers["worker-1"].is_accepting_calls

    @pytest.mark.asyncio
    async def test_ip_change_on_busy_pod_not_applied(self):
        """IP change on a pod mid-call must be silently ignored.

        K8s can recycle StatefulSet pod names; updating the IP while a call is
        live would redirect health checks to the replacement pod, masking the
        stale assignment on the original.
        """
        pool = _make_pool()
        pool._v1 = _mock_k8s_api([_make_pod("worker-1", "10.0.0.1")])
        await pool.discover_workers()
        pool.workers["worker-1"].current_call_sid = "CALL-ACTIVE"

        # Same pod name, new IP — simulates StatefulSet pod replacement
        pool._v1 = _mock_k8s_api([_make_pod("worker-1", "10.0.0.99")])
        await pool.discover_workers()

        assert pool.workers["worker-1"].private_ip == "10.0.0.1"
        assert pool.workers["worker-1"].host == "10.0.0.1:8765"
        assert pool.workers["worker-1"].current_call_sid == "CALL-ACTIVE"

    @pytest.mark.asyncio
    async def test_ip_change_on_idle_pod_is_applied(self):
        """IP change on an idle pod (no active call) must update private_ip and host."""
        pool = _make_pool()
        pool._v1 = _mock_k8s_api([_make_pod("worker-1", "10.0.0.1")])
        await pool.discover_workers()
        # worker has no active call — safe to update IP

        pool._v1 = _mock_k8s_api([_make_pod("worker-1", "10.0.0.99")])
        await pool.discover_workers()

        assert pool.workers["worker-1"].private_ip == "10.0.0.99"
        assert pool.workers["worker-1"].host == "10.0.0.99:8765"

    @pytest.mark.asyncio
    async def test_all_pods_terminating_removes_idle_workers(self):
        """If all pods have deletionTimestamp and none are handling calls, pool empties."""
        pool = _make_pool()
        pool._v1 = _mock_k8s_api(
            [
                _make_pod("worker-1", "10.0.0.1"),
                _make_pod("worker-2", "10.0.0.2"),
            ]
        )
        await pool.discover_workers()
        assert len(pool.workers) == 2

        pool._v1 = _mock_k8s_api(
            [
                _make_pod(
                    "worker-1", "10.0.0.1", deletion_timestamp="2024-01-01T00:00:00Z"
                ),
                _make_pod(
                    "worker-2", "10.0.0.2", deletion_timestamp="2024-01-01T00:00:00Z"
                ),
            ]
        )
        await pool.discover_workers()

        assert len(pool.workers) == 0

    @pytest.mark.asyncio
    async def test_new_pod_added_alongside_existing_busy_pod(self):
        """A new ready pod can be added while another pod is mid-call."""
        pool = _make_pool()
        pool._v1 = _mock_k8s_api([_make_pod("worker-1", "10.0.0.1")])
        await pool.discover_workers()
        pool.workers["worker-1"].current_call_sid = "CALL-XYZ"

        # worker-2 comes up while worker-1 is busy
        pool._v1 = _mock_k8s_api(
            [
                _make_pod("worker-1", "10.0.0.1"),
                _make_pod("worker-2", "10.0.0.2"),
            ]
        )
        await pool.discover_workers()

        assert "worker-2" in pool.workers
        assert pool.workers["worker-2"].is_accepting_calls
        # worker-1's call state must be preserved
        assert pool.workers["worker-1"].current_call_sid == "CALL-XYZ"


# ---------------------------------------------------------------------------
# discover_workers: error handling
# ---------------------------------------------------------------------------


class TestDiscoverWorkersErrorHandling:
    @pytest.mark.asyncio
    async def test_api_exception_does_not_raise(self):
        pool = _make_pool()

        v1 = AsyncMock()
        v1.list_namespaced_pod = AsyncMock(
            side_effect=_ApiException(status=403, reason="Forbidden")
        )
        pool._v1 = v1

        await pool.discover_workers()  # Must not raise

    @pytest.mark.asyncio
    async def test_generic_exception_does_not_raise(self):
        pool = _make_pool()

        v1 = AsyncMock()
        v1.list_namespaced_pod = AsyncMock(side_effect=RuntimeError("boom"))
        pool._v1 = v1

        await pool.discover_workers()  # Must not raise


# ---------------------------------------------------------------------------
# _teardown
# ---------------------------------------------------------------------------


class TestTeardown:
    @pytest.mark.asyncio
    async def test_teardown_closes_api_client(self):
        pool = _make_pool()
        mock_api_client = AsyncMock()
        pool._api_client = mock_api_client
        pool._v1 = MagicMock()

        await pool._teardown()

        mock_api_client.close.assert_called_once()
        assert pool._api_client is None
        assert pool._v1 is None

    @pytest.mark.asyncio
    async def test_teardown_noop_when_not_initialized(self):
        pool = _make_pool()
        await pool._teardown()  # Should not raise


# ---------------------------------------------------------------------------
# get_ws_url: K8s bug-fix regression — template works without public_ip
# ---------------------------------------------------------------------------


class TestGetWsUrlK8sTemplate:
    def test_template_with_instance_id_no_public_ip(self):
        """K8s pods have no public IP; WORKER_PUBLIC_WS_HOST_TEMPLATE must still work."""
        from app.services.worker_pool.base import WorkerStatus

        with (
            patch("app.services.worker_pool.base.PUBLIC_WS_URL", ""),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_SCHEME", "wss"),
            patch(
                "app.services.worker_pool.base.WORKER_PUBLIC_WS_HOST_TEMPLATE",
                "{instance_id}.workers.example.com",
            ),
            patch("app.services.worker_pool.base.WORKER_PUBLIC_WS_PORT", 443),
        ):
            w = WorkerStatus(
                host="10.0.0.1:8765",
                instance_id="worker-pod-abc",
                private_ip="10.0.0.1",
                public_ip=None,
            )
            url = w.get_ws_url("/ws")

        assert url == "wss://worker-pod-abc.workers.example.com/ws"

    def test_template_not_used_when_public_ws_url_set(self):
        """PUBLIC_WS_URL takes priority over template."""
        from app.services.worker_pool.base import WorkerStatus

        with (
            patch(
                "app.services.worker_pool.base.PUBLIC_WS_URL",
                "wss://shared.example.com",
            ),
            patch(
                "app.services.worker_pool.base.WORKER_PUBLIC_WS_HOST_TEMPLATE",
                "{instance_id}.workers.example.com",
            ),
        ):
            w = WorkerStatus(host="10.0.0.1:8765", instance_id="worker-pod-abc")
            url = w.get_ws_url("/ws")

        assert url == "wss://shared.example.com/ws"
