from loguru import logger

from app.config import (
    REDIS_HOST,
    REDIS_PORT,
    WORKER_HOSTS,
    WORKER_K8S_LABEL_SELECTOR,
    WORKER_K8S_NAMESPACE,
    WORKER_POOL_TYPE,
    WORKER_PORT,
)
from app.services.worker_pool.base import BaseWorkerPool
from app.services.worker_pool.ec2 import EC2WorkerPool
from app.services.worker_pool.k8s import K8sWorkerPool
from app.services.worker_pool.local import LocalWorkerPool
from app.services.worker_pool.redis_state import RedisStateBackend


def create_worker_pool() -> BaseWorkerPool:
    """Select and configure a worker pool based on WORKER_POOL_TYPE.

    WORKER_POOL_TYPE  │ Pool            │ Use case
    ──────────────────┼─────────────────┼──────────────────────────────────────
    ec2  (default)    │ EC2WorkerPool   │ Production (EC2 + Terraform)
    local             │ LocalWorkerPool │ Local dev / docker-compose
    k8s               │ K8sWorkerPool   │ EKS V2

    Set REDIS_HOST to enable shared assignment state for multi-runner EKS
    deployments. Leave it unset for single-runner setups.
    """
    if WORKER_POOL_TYPE == "k8s":
        logger.info(
            f"Using K8sWorkerPool "
            f"(namespace={WORKER_K8S_NAMESPACE}, selector={WORKER_K8S_LABEL_SELECTOR})"
        )
        pool: BaseWorkerPool = K8sWorkerPool(
            namespace=WORKER_K8S_NAMESPACE,
            label_selector=WORKER_K8S_LABEL_SELECTOR,
            port=WORKER_PORT,
        )
    elif WORKER_POOL_TYPE == "local":
        logger.info("Using LocalWorkerPool")
        pool = LocalWorkerPool(WORKER_HOSTS)
    else:
        if WORKER_POOL_TYPE != "ec2":
            logger.warning(
                f"Unknown WORKER_POOL_TYPE={WORKER_POOL_TYPE!r}, falling back to EC2WorkerPool"
            )
        logger.info("Using EC2WorkerPool")
        pool = EC2WorkerPool()

    if REDIS_HOST:
        pool._redis = RedisStateBackend(REDIS_HOST, REDIS_PORT)
        logger.info(
            f"Redis state backend enabled ({REDIS_HOST}:{REDIS_PORT}) — "
            f"multi-runner assignment coordination active"
        )

    return pool
