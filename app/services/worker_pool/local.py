from loguru import logger

from app.core.tracing import trace_class
from app.services.worker_pool.base import BaseWorkerPool, WorkerStatus


@trace_class(prefix="worker_pool")
class LocalWorkerPool(BaseWorkerPool):
    def __init__(self, worker_hosts: list[str]):
        super().__init__()
        self._initial_hosts = [h.strip() for h in worker_hosts if h.strip()]
        if not self._initial_hosts:
            logger.warning(
                "LocalWorkerPool initialised with no valid hosts. "
                "Set WORKER_HOSTS=host:port to fix this."
            )

    async def discover_workers(self) -> None:
        async with self._lock:
            if not self.workers:
                for host in self._initial_hosts:
                    self.workers[host] = WorkerStatus(host=host)
                logger.info(
                    f"Initialized {len(self.workers)} local workers: {self._initial_hosts}"
                )
