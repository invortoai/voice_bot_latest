from loguru import logger

from app.config import AWS_REGION, WORKER_POOL_TAG, WORKER_PORT
from app.core.tracing import trace_class
from app.services.worker_pool.base import BaseWorkerPool, WorkerStatus


@trace_class(prefix="worker_pool")
class EC2WorkerPool(BaseWorkerPool):
    def __init__(self):
        super().__init__()
        self._ec2_client = None

    def _get_ec2_client(self):
        if self._ec2_client is None:
            import boto3

            self._ec2_client = boto3.client("ec2", region_name=AWS_REGION)
        return self._ec2_client

    async def discover_workers(self) -> None:
        try:
            ec2 = self._get_ec2_client()
            response = ec2.describe_instances(
                Filters=[
                    {"Name": "tag:Type", "Values": [WORKER_POOL_TAG]},
                    {"Name": "instance-state-name", "Values": ["running"]},
                ]
            )

            discovered: dict[str, WorkerStatus] = {}
            for reservation in response["Reservations"]:
                for instance in reservation["Instances"]:
                    instance_id = instance["InstanceId"]
                    private_ip = instance.get("PrivateIpAddress")
                    public_ip = instance.get("PublicIpAddress")
                    if private_ip:
                        discovered[instance_id] = WorkerStatus(
                            host=f"{private_ip}:{WORKER_PORT}",
                            instance_id=instance_id,
                            private_ip=private_ip,
                            public_ip=public_ip,
                        )

            # --- Add / update discovered instances ---
            async with self._lock:
                for instance_id, worker in discovered.items():
                    if instance_id not in self.workers:
                        self.workers[instance_id] = worker
                        logger.info(
                            f"Discovered new worker: {instance_id} ({worker.private_ip})"
                        )
                    else:
                        existing = self.workers[instance_id]
                        existing.private_ip = worker.private_ip
                        existing.public_ip = worker.public_ip
                        existing.host = worker.host

                stale_instance_ids = [i for i in self.workers if i not in discovered]

            # --- Remove stale instances (drain-safe: check Redis for active calls) ---
            # _has_active_call checks Redis when local current_call_sid is None,
            # covering the runner-restart case where local cache is blank but Redis
            # still records an active assignment.
            for instance_id in stale_instance_ids:
                async with self._lock:
                    existing = self.workers.get(instance_id)
                if not existing:
                    continue

                has_call = await self._has_active_call(existing)
                if has_call:
                    logger.info(
                        f"EC2 instance {instance_id} gone but still handling call "
                        f"{existing.current_call_sid or '(Redis)'} — keeping until call ends."
                    )
                else:
                    async with self._lock:
                        if instance_id in self.workers:
                            logger.info(f"Removing stale worker: {instance_id}")
                            del self.workers[instance_id]

            logger.debug(f"Worker pool: {len(discovered)} workers discovered via EC2")
        except Exception as e:
            logger.error(f"EC2 worker discovery failed: {e}")
