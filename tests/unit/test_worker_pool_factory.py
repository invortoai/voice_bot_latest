"""Unit tests for create_worker_pool() factory (app/services/worker_pool/factory.py).

Verifies that WORKER_POOL_TYPE selects the right pool class and that REDIS_HOST
triggers RedisStateBackend injection.
"""

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_pool(**env_overrides):
    """Call create_worker_pool() with patched config values."""
    defaults = {
        "app.services.worker_pool.factory.WORKER_POOL_TYPE": "ec2",
        "app.services.worker_pool.factory.REDIS_HOST": "",
        "app.services.worker_pool.factory.REDIS_PORT": 6379,
        "app.services.worker_pool.factory.WORKER_HOSTS": ["localhost:8765"],
        "app.services.worker_pool.factory.WORKER_K8S_NAMESPACE": "invorto",
        "app.services.worker_pool.factory.WORKER_K8S_LABEL_SELECTOR": "app=worker",
        "app.services.worker_pool.factory.WORKER_PORT": 8765,
    }
    defaults.update(env_overrides)

    with patch.multiple(
        "app.services.worker_pool.factory",
        **{k.split(".")[-1]: v for k, v in defaults.items()},
    ):
        from app.services.worker_pool.factory import create_worker_pool

        return create_worker_pool()


# ---------------------------------------------------------------------------
# Pool type selection
# ---------------------------------------------------------------------------


class TestPoolTypeSelection:
    def test_ec2_type_creates_ec2_pool(self):
        from app.services.worker_pool.ec2 import EC2WorkerPool

        with (
            patch("app.services.worker_pool.factory.WORKER_POOL_TYPE", "ec2"),
            patch("app.services.worker_pool.factory.REDIS_HOST", ""),
        ):
            from app.services.worker_pool.factory import create_worker_pool

            pool = create_worker_pool()

        assert isinstance(pool, EC2WorkerPool)

    def test_local_type_creates_local_pool(self):
        from app.services.worker_pool.local import LocalWorkerPool

        with (
            patch("app.services.worker_pool.factory.WORKER_POOL_TYPE", "local"),
            patch("app.services.worker_pool.factory.WORKER_HOSTS", ["localhost:8765"]),
            patch("app.services.worker_pool.factory.REDIS_HOST", ""),
        ):
            from app.services.worker_pool.factory import create_worker_pool

            pool = create_worker_pool()

        assert isinstance(pool, LocalWorkerPool)

    def test_k8s_type_creates_k8s_pool(self):
        from app.services.worker_pool.k8s import K8sWorkerPool

        with (
            patch("app.services.worker_pool.factory.WORKER_POOL_TYPE", "k8s"),
            patch("app.services.worker_pool.factory.WORKER_K8S_NAMESPACE", "invorto"),
            patch(
                "app.services.worker_pool.factory.WORKER_K8S_LABEL_SELECTOR",
                "app=worker",
            ),
            patch("app.services.worker_pool.factory.WORKER_PORT", 8765),
            patch("app.services.worker_pool.factory.REDIS_HOST", ""),
        ):
            from app.services.worker_pool.factory import create_worker_pool

            pool = create_worker_pool()

        assert isinstance(pool, K8sWorkerPool)

    def test_unknown_type_falls_back_to_ec2(self):
        from app.services.worker_pool.ec2 import EC2WorkerPool

        with (
            patch("app.services.worker_pool.factory.WORKER_POOL_TYPE", "nonexistent"),
            patch("app.services.worker_pool.factory.REDIS_HOST", ""),
        ):
            from app.services.worker_pool.factory import create_worker_pool

            pool = create_worker_pool()

        assert isinstance(pool, EC2WorkerPool)


# ---------------------------------------------------------------------------
# K8s pool configuration
# ---------------------------------------------------------------------------


class TestK8sPoolConfiguration:
    def test_k8s_pool_gets_correct_namespace(self):
        with (
            patch("app.services.worker_pool.factory.WORKER_POOL_TYPE", "k8s"),
            patch(
                "app.services.worker_pool.factory.WORKER_K8S_NAMESPACE", "my-namespace"
            ),
            patch(
                "app.services.worker_pool.factory.WORKER_K8S_LABEL_SELECTOR",
                "app=worker",
            ),
            patch("app.services.worker_pool.factory.WORKER_PORT", 8765),
            patch("app.services.worker_pool.factory.REDIS_HOST", ""),
        ):
            from app.services.worker_pool.factory import create_worker_pool

            pool = create_worker_pool()

        assert pool._namespace == "my-namespace"

    def test_k8s_pool_gets_correct_label_selector(self):
        with (
            patch("app.services.worker_pool.factory.WORKER_POOL_TYPE", "k8s"),
            patch("app.services.worker_pool.factory.WORKER_K8S_NAMESPACE", "invorto"),
            patch(
                "app.services.worker_pool.factory.WORKER_K8S_LABEL_SELECTOR",
                "env=prod,role=worker",
            ),
            patch("app.services.worker_pool.factory.WORKER_PORT", 8765),
            patch("app.services.worker_pool.factory.REDIS_HOST", ""),
        ):
            from app.services.worker_pool.factory import create_worker_pool

            pool = create_worker_pool()

        assert pool._label_selector == "env=prod,role=worker"

    def test_k8s_pool_gets_correct_port(self):
        with (
            patch("app.services.worker_pool.factory.WORKER_POOL_TYPE", "k8s"),
            patch("app.services.worker_pool.factory.WORKER_K8S_NAMESPACE", "invorto"),
            patch(
                "app.services.worker_pool.factory.WORKER_K8S_LABEL_SELECTOR",
                "app=worker",
            ),
            patch("app.services.worker_pool.factory.WORKER_PORT", 9999),
            patch("app.services.worker_pool.factory.REDIS_HOST", ""),
        ):
            from app.services.worker_pool.factory import create_worker_pool

            pool = create_worker_pool()

        assert pool._port == 9999


# ---------------------------------------------------------------------------
# Redis injection
# ---------------------------------------------------------------------------


class TestRedisInjection:
    def test_redis_not_injected_when_redis_host_empty(self):
        with (
            patch("app.services.worker_pool.factory.WORKER_POOL_TYPE", "local"),
            patch("app.services.worker_pool.factory.WORKER_HOSTS", ["localhost:8765"]),
            patch("app.services.worker_pool.factory.REDIS_HOST", ""),
        ):
            from app.services.worker_pool.factory import create_worker_pool

            pool = create_worker_pool()

        assert pool._redis is None

    def test_redis_injected_when_redis_host_set(self):
        from app.services.worker_pool.redis_state import RedisStateBackend

        mock_backend = MagicMock(spec=RedisStateBackend)

        with (
            patch("app.services.worker_pool.factory.WORKER_POOL_TYPE", "local"),
            patch("app.services.worker_pool.factory.WORKER_HOSTS", ["localhost:8765"]),
            patch("app.services.worker_pool.factory.REDIS_HOST", "redis.example.com"),
            patch("app.services.worker_pool.factory.REDIS_PORT", 6379),
            patch(
                "app.services.worker_pool.factory.RedisStateBackend",
                return_value=mock_backend,
            ),
        ):
            from app.services.worker_pool.factory import create_worker_pool

            pool = create_worker_pool()

        assert pool._redis is mock_backend

    def test_redis_backend_created_with_correct_host_port(self):
        captured = {}

        def capture_redis(host, port):
            captured["host"] = host
            captured["port"] = port
            return MagicMock()

        with (
            patch("app.services.worker_pool.factory.WORKER_POOL_TYPE", "ec2"),
            patch("app.services.worker_pool.factory.REDIS_HOST", "my-redis.internal"),
            patch("app.services.worker_pool.factory.REDIS_PORT", 6380),
            patch(
                "app.services.worker_pool.factory.RedisStateBackend",
                side_effect=capture_redis,
            ),
        ):
            from app.services.worker_pool.factory import create_worker_pool

            create_worker_pool()

        assert captured["host"] == "my-redis.internal"
        assert captured["port"] == 6380


# ---------------------------------------------------------------------------
# LocalWorkerPool host wiring
# ---------------------------------------------------------------------------


class TestLocalWorkerPoolHosts:
    def test_local_pool_receives_worker_hosts(self):
        with (
            patch("app.services.worker_pool.factory.WORKER_POOL_TYPE", "local"),
            patch(
                "app.services.worker_pool.factory.WORKER_HOSTS", ["h1:8765", "h2:8765"]
            ),
            patch("app.services.worker_pool.factory.REDIS_HOST", ""),
        ):
            from app.services.worker_pool.factory import create_worker_pool

            pool = create_worker_pool()

        assert pool._initial_hosts == ["h1:8765", "h2:8765"]
