from typing import Optional

from loguru import logger

from app.config import WORKER_PORT
from app.core.tracing import trace_class
from app.services.worker_pool.base import BaseWorkerPool, WorkerStatus


@trace_class(prefix="worker_pool")
class K8sWorkerPool(BaseWorkerPool):
    """Discovers workers via the Kubernetes API using in-cluster service-account auth.

    Only pods with ContainersReady=True and no deletionTimestamp are admitted.
    This prevents routing calls to pods still running init containers (e.g. fetching
    secrets from Secrets Manager) or undergoing graceful shutdown — the primary
    failure mode with KEDA scale-from-zero.
    """

    def __init__(self, namespace: str, label_selector: str, port: int) -> None:
        super().__init__()
        self._namespace = namespace
        self._label_selector = label_selector
        self._port = port
        self._api_client = None
        self._v1 = None

    def _ensure_client(self):
        if self._v1 is None:
            from kubernetes_asyncio import client as k8s_client, config as k8s_config

            k8s_config.load_incluster_config()
            self._api_client = k8s_client.ApiClient()
            self._v1 = k8s_client.CoreV1Api(self._api_client)
        return self._v1

    async def discover_workers(self) -> None:
        from kubernetes_asyncio.client.exceptions import ApiException

        try:
            v1 = self._ensure_client()
            pod_list = await v1.list_namespaced_pod(
                namespace=self._namespace,
                label_selector=self._label_selector,
                field_selector="status.phase=Running",
            )

            discovered: dict[str, WorkerStatus] = {}
            for pod in pod_list.items:
                if pod.metadata.deletion_timestamp:
                    continue
                pod_ip = pod.status.pod_ip
                if not pod_ip:
                    continue
                conditions = {c.type: c.status for c in (pod.status.conditions or [])}
                if conditions.get("ContainersReady") != "True":
                    continue

                pod_name = pod.metadata.name
                discovered[pod_name] = WorkerStatus(
                    host=f"{pod_ip}:{self._port}",
                    instance_id=pod_name,
                    private_ip=pod_ip,
                    # Workers in EKS have no public IP; WebSocket routing goes through
                    # Ingress. Set WORKER_PUBLIC_WS_HOST_TEMPLATE={instance_id}.workers.example.com
                    # so get_ws_url() can build per-pod URLs without a public IP.
                    public_ip=None,
                )

            # --- Add / update discovered pods ---
            async with self._lock:
                for pod_name, worker in discovered.items():
                    if pod_name not in self.workers:
                        self.workers[pod_name] = worker
                        logger.info(
                            f"Discovered worker: {pod_name} ({worker.private_ip})"
                        )
                    else:
                        existing = self.workers[pod_name]
                        if existing.private_ip != worker.private_ip:
                            if existing.current_call_sid is not None:
                                # IP change on a pod mid-call means the pod was replaced
                                # (K8s recycled the name, e.g. StatefulSet). Updating the
                                # IP would redirect health checks to the new pod which
                                # doesn't own the call, masking the stale assignment.
                                # Leave it: the broken WebSocket triggers a hangup →
                                # status webhook → release_worker() clears normally.
                                logger.warning(
                                    f"Pod {pod_name} IP changed from {existing.private_ip} "
                                    f"to {worker.private_ip} while handling call "
                                    f"{existing.current_call_sid} — NOT updating IP."
                                )
                            else:
                                logger.info(
                                    f"Pod {pod_name} IP updated: "
                                    f"{existing.private_ip} → {worker.private_ip}"
                                )
                                existing.private_ip = worker.private_ip
                                existing.host = worker.host

                # Collect stale pod names while holding the lock, then check outside.
                stale_pod_names = [n for n in self.workers if n not in discovered]

            # --- Remove stale pods (drain-safe: check Redis for active calls) ---
            # _has_active_call checks Redis when local current_call_sid is None,
            # covering the runner-restart case where local cache is blank but Redis
            # still records an active assignment.
            for pod_name in stale_pod_names:
                async with self._lock:
                    existing = self.workers.get(pod_name)
                if not existing:
                    continue

                has_call = await self._has_active_call(existing)
                if has_call:
                    logger.info(
                        f"Pod {pod_name} gone from K8s but still handling call "
                        f"{existing.current_call_sid or '(Redis)'} — keeping until call ends."
                    )
                else:
                    async with self._lock:
                        if pod_name in self.workers:
                            logger.info(f"Removing stale worker: {pod_name}")
                            del self.workers[pod_name]

            logger.debug(
                f"Worker pool: {len(discovered)} workers discovered via K8s API"
            )

        except ApiException as e:
            logger.error(
                f"K8s API error during worker discovery: {e.status} {e.reason}"
            )
        except Exception as e:
            logger.error(f"Worker discovery failed: {e}")

    async def _teardown(self) -> None:
        if self._api_client:
            await self._api_client.close()
            self._api_client = None
            self._v1 = None
