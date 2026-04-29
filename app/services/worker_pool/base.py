import asyncio
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

import httpx
from loguru import logger

from app.config import (
    API_KEY,
    HEALTH_CHECK_INTERVAL,
    PUBLIC_WS_URL,
    WORKER_AUTH_TOKEN,
    WORKER_PORT,
    WORKER_PUBLIC_WS_HOST_SUFFIX,
    WORKER_PUBLIC_WS_HOST_TEMPLATE,
    WORKER_PUBLIC_WS_PORT,
    WORKER_PUBLIC_WS_SCHEME,
    WORKER_STALE_ASSIGNMENT_SECONDS,
    WORKER_TIMEOUT,
)

if TYPE_CHECKING:
    from app.services.worker_pool.redis_state import RedisStateBackend


class WorkerStatus:
    def __init__(
        self,
        host: str,
        instance_id: Optional[str] = None,
        private_ip: Optional[str] = None,
        public_ip: Optional[str] = None,
    ):
        self.host = host
        self.instance_id = instance_id or host
        self.private_ip = private_ip
        self.public_ip = public_ip
        # Best-effort local cache of assignment state. Written by assign/release/reassign
        # operations on THIS runner. Never synced from Redis — Redis is authoritative.
        # Used for drain safety and as a pre-filter before the atomic Lua assignment.
        self.current_call_sid: Optional[str] = None
        self.assigned_at: Optional[float] = None
        self.last_health_check = time.time()
        self.consecutive_failures = 0

    @property
    def is_accepting_calls(self) -> bool:
        """True iff this runner believes the worker can take a new call.

        Checks both local assignment cache and health state. The Redis Lua script
        performs the final atomic check — this is a pre-filter only.
        """
        return self.current_call_sid is None and self.consecutive_failures < 3

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "instance_id": self.instance_id,
            "private_ip": self.private_ip,
            "public_ip": self.public_ip,
            "is_available": self.is_accepting_calls,
            "current_call_sid": self.current_call_sid,
            "assigned_at": self.assigned_at,
            "last_health_check": self.last_health_check,
        }

    def to_safe_dict(self) -> dict:
        return {
            "worker_id": self.instance_id,
            "is_available": self.is_accepting_calls,
            "current_call_sid": self.current_call_sid,
            "assigned_at": self.assigned_at,
            "last_health_check": self.last_health_check,
        }

    def get_health_url(self) -> str:
        if self.private_ip:
            return f"http://{self.private_ip}:{WORKER_PORT}/health"
        return f"https://{self.host}/health"

    def get_ws_url(self, path: str = "/ws") -> str:
        """Build the public WebSocket URL for this worker.

        Resolution order:
        1. PUBLIC_WS_URL — shared base URL (ngrok in dev, or a load-balanced proxy)
        2. WORKER_PUBLIC_WS_HOST_TEMPLATE — per-worker template; checked before public_ip
           so K8s pods (no public IP) can still use {instance_id} for per-pod Ingress routing
        3. public_ip + WORKER_PUBLIC_WS_HOST_SUFFIX — EC2 sslip.io default
        4. private_ip / host — local dev fallback
        """
        if not path.startswith("/"):
            path = "/" + path

        if PUBLIC_WS_URL:
            base = PUBLIC_WS_URL.rstrip("/")
            if not (base.startswith("ws://") or base.startswith("wss://")):
                base = f"{WORKER_PUBLIC_WS_SCHEME}://{base}"
            return base + path

        if WORKER_PUBLIC_WS_HOST_TEMPLATE:
            public_host = WORKER_PUBLIC_WS_HOST_TEMPLATE.format(
                public_ip=self.public_ip or "",
                private_ip=self.private_ip or "",
                instance_id=self.instance_id or "",
                host=self.host or "",
            )
            port_part = (
                "" if WORKER_PUBLIC_WS_PORT in (0, 443) else f":{WORKER_PUBLIC_WS_PORT}"
            )
            return f"{WORKER_PUBLIC_WS_SCHEME}://{public_host}{port_part}{path}"

        if self.public_ip:
            public_host = f"{self.public_ip}{WORKER_PUBLIC_WS_HOST_SUFFIX}"
            port_part = (
                "" if WORKER_PUBLIC_WS_PORT in (0, 443) else f":{WORKER_PUBLIC_WS_PORT}"
            )
            return f"{WORKER_PUBLIC_WS_SCHEME}://{public_host}{port_part}{path}"

        if self.private_ip:
            return f"{WORKER_PUBLIC_WS_SCHEME}://{self.private_ip}:{WORKER_PORT}{path}"
        return f"{WORKER_PUBLIC_WS_SCHEME}://{self.host}{path}"


class BaseWorkerPool(ABC):
    # Minimum seconds between on-demand discovery calls triggered by a "no
    # workers available" response.
    _ON_DEMAND_DISCOVERY_COOLDOWN = 5

    # Grace window after assignment before the health-check body reader will
    # treat a "current_call=null" response as a stale assignment.
    # Must be longer than the worst-case "assignment → WS accepted → start_call"
    # path.  Jambonz can take up to ~65 s in slow-DNS/LB edge cases.
    _STARTUP_GRACE_SECONDS = 90

    # How many times release_worker retries the Redis call-mapping lookup before
    # giving up. Covers the RC-8 race where the status webhook fires in the ~50 ms
    # window between Twilio call creation and reassign_call_sid writing the key.
    _RELEASE_LOOKUP_RETRIES = 4
    _RELEASE_LOOKUP_DELAY = 0.2  # seconds between retries

    # Redis assignment retries on transient error (network blip, timeout).
    _REDIS_ASSIGN_RETRIES = 3
    _REDIS_ASSIGN_RETRY_DELAY = 0.5

    def __init__(self):
        self.workers: dict[str, WorkerStatus] = {}
        self._lock = asyncio.Lock()
        self._health_check_task: Optional[asyncio.Task] = None
        # Injected by create_worker_pool() when REDIS_HOST is configured.
        self._redis: Optional["RedisStateBackend"] = None
        self._last_on_demand_discovery: float = 0
        self._discovery_lock = asyncio.Lock()

    @property
    def _redis_key_ttl(self) -> int:
        """TTL (seconds) for Redis keys.  24 h outlasts any real call; actual stale
        detection is done by health-check body reader and _release_stale_assignments,
        not by key expiry.
        """
        return 86400

    @abstractmethod
    async def discover_workers(self) -> None:
        pass

    async def _teardown(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Drain-safety helper
    # ------------------------------------------------------------------

    async def _has_active_call(self, worker: WorkerStatus) -> bool:
        """True if the worker has an active call — checks Redis when local cache is empty.

        Used by subclass discover_workers() for drain safety: keep a worker in the pool
        even after it disappears from the discovery API if it still owns a call.
        Falls back to local cache when Redis is not configured.
        """
        if worker.current_call_sid is not None:
            return True
        if self._redis:
            try:
                state = await self._redis.get_worker_state(worker.instance_id)
                return bool(state.get("current_call_sid"))
            except Exception as exc:
                logger.warning(
                    f"Redis get_worker_state failed for {worker.instance_id} "
                    f"during drain check: {exc} — assuming busy (safe default)."
                )
                return True
        return False

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check_worker(self, worker: WorkerStatus) -> bool:
        try:
            headers = {"X-API-Key": API_KEY} if API_KEY else {}
            async with httpx.AsyncClient(timeout=WORKER_TIMEOUT) as client:
                response = await client.get(worker.get_health_url(), headers=headers)

            if response.status_code == 200:
                try:
                    body = response.json()
                    worker_reports_free = (
                        "current_call" in body and body["current_call"] is None
                    )
                except Exception:
                    body = {}
                    worker_reports_free = False

                async with self._lock:
                    previous_failures = worker.consecutive_failures
                    worker.last_health_check = time.time()
                    worker.consecutive_failures = 0

                    if worker.current_call_sid is not None:
                        age = (
                            time.time() - worker.assigned_at
                            if worker.assigned_at is not None
                            else self._STARTUP_GRACE_SECONDS + 1
                        )

                        if worker_reports_free:
                            if age <= self._STARTUP_GRACE_SECONDS:
                                logger.debug(
                                    f"Worker {worker.instance_id} reports free but assignment is "
                                    f"only {age:.1f}s old — skipping stale release (startup grace)."
                                )
                            else:
                                stale_sid = worker.current_call_sid
                                stale_worker_id = worker.instance_id
                                worker.current_call_sid = None
                                worker.assigned_at = None
                                logger.warning(
                                    f"Worker {worker.instance_id} reports no active call but "
                                    f"runner had stale assignment call_sid={stale_sid} — "
                                    f"releasing (missed status webhook or multi-runner skew)."
                                )
                                if self._redis:
                                    asyncio.create_task(
                                        self._redis.release_assignment(
                                            stale_worker_id, stale_sid
                                        )
                                    )
                        else:
                            # Worker is busy — check for SID mismatch (can happen after
                            # reassignment while the worker is still connecting new WS).
                            worker_reported_sid = body.get("current_call")
                            if (
                                worker_reported_sid is not None
                                and worker_reported_sid != worker.current_call_sid
                            ):
                                if age <= self._STARTUP_GRACE_SECONDS:
                                    logger.debug(
                                        f"Worker {worker.instance_id} reports SID="
                                        f"{worker_reported_sid} but runner has "
                                        f"{worker.current_call_sid} — within grace "
                                        f"({age:.1f}s), ignoring."
                                    )
                                else:
                                    logger.warning(
                                        f"Worker {worker.instance_id} reports "
                                        f"call_sid={worker_reported_sid} but runner has "
                                        f"{worker.current_call_sid} — updating local cache "
                                        f"to match worker (missed SID reassignment)."
                                    )
                                    worker.current_call_sid = worker_reported_sid

                if previous_failures > 0:
                    logger.info(
                        f"Worker health recovered: {worker.instance_id} "
                        f"after {previous_failures} consecutive failures"
                    )
                return True

            async with self._lock:
                previous_failures = worker.consecutive_failures
                worker.consecutive_failures += 1
                current_failures = worker.consecutive_failures
            logger.warning(
                f"Health probe failed: {worker.instance_id} "
                f"status={response.status_code} consecutive_failures={current_failures}"
            )
            if previous_failures < 3 <= current_failures:
                logger.warning(
                    f"Worker marked unhealthy: {worker.instance_id} "
                    f"({current_failures} consecutive failures)"
                )

        except Exception as e:
            logger.debug(f"Health check failed for {worker.instance_id}: {e}")
            async with self._lock:
                previous_failures = worker.consecutive_failures
                worker.consecutive_failures += 1
                current_failures = worker.consecutive_failures
            logger.warning(
                f"Health probe failed: {worker.instance_id} "
                f"({type(e).__name__}) consecutive_failures={current_failures}"
            )
            if previous_failures < 3 <= current_failures:
                logger.warning(
                    f"Worker marked unhealthy: {worker.instance_id} "
                    f"({current_failures} consecutive failures)"
                )

        return False

    # ------------------------------------------------------------------
    # Health-check loop
    # ------------------------------------------------------------------

    async def run_health_checks(self) -> None:
        while True:
            try:
                await self.discover_workers()

                tasks = [self.health_check_worker(w) for w in self.workers.values()]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

                await self._release_stale_assignments()

                available = sum(1 for w in self.workers.values() if w.is_accepting_calls)
                logger.debug(
                    f"Health check: {available}/{len(self.workers)} workers available"
                )
            except Exception as e:
                logger.error(f"Error in health check loop: {e}")
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)

    # ------------------------------------------------------------------
    # Stale assignment reaping
    # ------------------------------------------------------------------

    async def _release_stale_assignments(self) -> None:
        if WORKER_STALE_ASSIGNMENT_SECONDS <= 0:
            return

        now = time.time()
        stale: list[WorkerStatus] = []
        async with self._lock:
            for w in self.workers.values():
                if (
                    w.current_call_sid is not None
                    and w.assigned_at is not None
                    and w.consecutive_failures == 0
                    and now - w.assigned_at > WORKER_STALE_ASSIGNMENT_SECONDS
                ):
                    stale.append(w)

        if not stale:
            return

        if self._redis:
            stale_ids = [w.instance_id for w in stale]
            try:
                states = await self._redis.batch_get_states(stale_ids)
            except Exception as e:
                logger.warning(f"Redis batch_get_states failed in stale release check: {e}")
                return

            for worker in stale:
                state = states.get(worker.instance_id, {})
                if not state.get("current_call_sid"):
                    age_s = int(now - worker.assigned_at) if worker.assigned_at else "?"
                    logger.warning(
                        f"Stale assignment: Redis shows {worker.instance_id} free "
                        f"(local call_sid={worker.current_call_sid}, age={age_s}s) — releasing."
                    )
                    async with self._lock:
                        worker.current_call_sid = None
                        worker.assigned_at = None
        else:
            for worker in stale:
                worker_confirmed_idle = False
                try:
                    headers = {"X-API-Key": API_KEY} if API_KEY else {}
                    async with httpx.AsyncClient(timeout=WORKER_TIMEOUT) as client:
                        resp = await client.get(worker.get_health_url(), headers=headers)
                    if resp.status_code == 200:
                        worker_confirmed_idle = resp.json().get("current_call") is None
                except Exception as health_err:
                    logger.warning(
                        f"Health check failed before stale release of "
                        f"{worker.instance_id}: {health_err} — skipping."
                    )
                    continue

                if not worker_confirmed_idle:
                    logger.warning(
                        f"Stale release skipped: {worker.instance_id} still reports "
                        f"an active call (runner has {worker.current_call_sid}) — retrying next cycle."
                    )
                    continue

                logger.warning(
                    f"Force-releasing stale worker {worker.instance_id} "
                    f"after {int(now - worker.assigned_at)}s "
                    f"(call_sid={worker.current_call_sid}) — worker confirmed idle."
                )
                async with self._lock:
                    worker.current_call_sid = None
                    worker.assigned_at = None

    # ------------------------------------------------------------------
    # On-demand discovery
    # ------------------------------------------------------------------

    async def _on_demand_discover(self) -> bool:
        """Run discover_workers() if the cooldown has elapsed.

        The _discovery_lock ensures concurrent callers share one discovery run.
        Returns True if discovery actually ran.
        """
        now = time.time()
        if now - self._last_on_demand_discovery < self._ON_DEMAND_DISCOVERY_COOLDOWN:
            async with self._discovery_lock:
                return False

        async with self._discovery_lock:
            if (
                time.time() - self._last_on_demand_discovery
                < self._ON_DEMAND_DISCOVERY_COOLDOWN
            ):
                return False
            self._last_on_demand_discovery = time.time()
            try:
                await self.discover_workers()
                logger.info("On-demand discovery completed")
                return True
            except Exception as e:
                logger.warning(f"On-demand discovery failed: {e}")
                return False

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------

    async def get_and_assign_worker(self, call_sid: str) -> Optional[WorkerStatus]:
        """Atomically find an available worker and claim it for call_sid.

        Redis path  — passes healthy local candidates to the Lua find_and_assign
        script; the script atomically checks+sets the Redis HASH, so no two runners
        can claim the same worker even under concurrent load.  Retries up to
        _REDIS_ASSIGN_RETRIES times on transient errors; rejects on exhaustion
        (falling through to local would cause split-brain).

        Local path (no Redis) — acquires asyncio.Lock for the full sequence.
        """
        if self._redis:
            for attempt in range(1, self._REDIS_ASSIGN_RETRIES + 1):
                try:
                    worker = await self._get_and_assign_worker_redis(call_sid)
                    if worker:
                        return worker

                    if await self._on_demand_discover():
                        worker = await self._get_and_assign_worker_redis(call_sid)
                        if worker:
                            return worker

                    return None
                except Exception as e:
                    if attempt < self._REDIS_ASSIGN_RETRIES:
                        delay = self._REDIS_ASSIGN_RETRY_DELAY * attempt
                        logger.warning(
                            f"Redis error during worker assignment for {call_sid} "
                            f"(attempt {attempt}/{self._REDIS_ASSIGN_RETRIES}): {e}. "
                            f"Retrying in {delay:.1f}s..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            f"Redis unavailable for worker assignment {call_sid} after "
                            f"{self._REDIS_ASSIGN_RETRIES} attempts: {e}. Rejecting call — "
                            f"falling back to local would cause split-brain."
                        )
            return None

        worker = await self._get_and_assign_worker_local(call_sid)
        if worker:
            return worker

        if await self._on_demand_discover():
            return await self._get_and_assign_worker_local(call_sid)

        return None

    async def _get_and_assign_worker_redis(
        self, call_sid: str
    ) -> Optional[WorkerStatus]:
        """Snapshot healthy candidates then atomically claim one via Lua."""
        async with self._lock:
            candidates = [
                w
                for w in self.workers.values()
                if w.current_call_sid is None and w.consecutive_failures < 3
            ]

        if not candidates:
            async with self._lock:
                total = len(self.workers)
                busy = sum(1 for w in self.workers.values() if w.current_call_sid is not None)
                unhealthy = sum(1 for w in self.workers.values() if w.consecutive_failures >= 3)
            logger.warning(
                f"No available workers for call {call_sid}: "
                f"total={total} busy={busy} unhealthy={unhealthy}"
            )
            return None

        candidate_ids = [w.instance_id for w in candidates]
        claimed_id = await self._redis.find_and_assign(
            candidate_ids, call_sid, self._redis_key_ttl
        )

        if not claimed_id:
            logger.warning(
                f"All {len(candidates)} local candidates were busy in Redis for {call_sid} "
                f"(stale local cache or concurrent multi-runner assignment)"
            )
            return None

        async with self._lock:
            worker = self.workers.get(claimed_id)
            if worker:
                worker.current_call_sid = call_sid
                worker.assigned_at = time.time()

        if worker is None:
            # Worker was removed from pool between snapshot and Lua claim (discover_workers
            # race). Release the orphaned Redis state so the slot isn't locked for 24 h.
            logger.warning(
                f"Claimed worker {claimed_id} is no longer in the local pool "
                f"(removed by concurrent discover_workers) — releasing Redis orphan for {call_sid}."
            )
            asyncio.create_task(self._redis.release_assignment(claimed_id, call_sid))
            return None

        logger.info(f"Worker assigned to call {call_sid}: {claimed_id} (via Redis)")
        return worker

    async def _get_and_assign_worker_local(
        self, call_sid: str
    ) -> Optional[WorkerStatus]:
        async with self._lock:
            for worker in self.workers.values():
                if worker.current_call_sid is None and worker.consecutive_failures < 3:
                    worker.current_call_sid = call_sid
                    worker.assigned_at = time.time()
                    logger.info(
                        f"Worker assigned to call {call_sid}: {worker.instance_id}"
                    )
                    return worker
            total = len(self.workers)
            busy = sum(1 for w in self.workers.values() if w.current_call_sid is not None)
            unhealthy = sum(1 for w in self.workers.values() if w.consecutive_failures >= 3)
            logger.warning(
                f"No available workers for call {call_sid}: "
                f"total={total} busy={busy} unhealthy={unhealthy}"
            )
        return None

    # ------------------------------------------------------------------
    # Worker lookup
    # ------------------------------------------------------------------

    async def get_worker_for_call(self, call_sid: str) -> Optional[WorkerStatus]:
        if self._redis:
            try:
                worker_id = await self._redis.get_worker_for_call(call_sid)
            except Exception as e:
                logger.warning(f"Redis error in get_worker_for_call for {call_sid}: {e}")
                worker_id = None

            if worker_id:
                async with self._lock:
                    worker = self.workers.get(worker_id)
                if worker is not None:
                    return worker
                logger.warning(
                    f"Redis has assignment for {call_sid} → {worker_id} but worker is "
                    f"not in local pool — forcing discovery."
                )
                await self.discover_workers()
                async with self._lock:
                    return self.workers.get(worker_id)
            return None

        async with self._lock:
            for worker in self.workers.values():
                if worker.current_call_sid == call_sid:
                    return worker
        return None

    # ------------------------------------------------------------------
    # SID reassignment (outbound: temp UUID → real Twilio call SID)
    # ------------------------------------------------------------------

    async def reassign_call_sid(self, old_call_sid: str, new_call_sid: str) -> None:
        if self._redis:
            try:
                worker_id = await self._redis.get_worker_for_call(old_call_sid)
            except Exception as e:
                logger.error(f"Redis error in reassign_call_sid: {e}")
                worker_id = None

            if worker_id:
                try:
                    await self._redis.reassign(
                        old_call_sid, new_call_sid, worker_id, self._redis_key_ttl
                    )
                except Exception as e:
                    logger.error(
                        f"Redis error during reassign {old_call_sid} → {new_call_sid}: {e}"
                    )

                async with self._lock:
                    worker = self.workers.get(worker_id)
                    if worker:
                        worker.current_call_sid = new_call_sid

                logger.info(
                    f"Call SID reassigned on worker {worker_id}: "
                    f"{old_call_sid} → {new_call_sid}"
                )
            else:
                logger.warning(
                    f"Reassign failed: no Redis assignment for {old_call_sid} "
                    f"(new_call_sid={new_call_sid})"
                )
            return

        async with self._lock:
            for worker in self.workers.values():
                if worker.current_call_sid == old_call_sid:
                    worker.current_call_sid = new_call_sid
                    logger.info(
                        f"Call SID reassigned on worker {worker.instance_id}: "
                        f"{old_call_sid} → {new_call_sid}"
                    )
                    return
        logger.warning(
            f"Reassign failed: no worker found for {old_call_sid} "
            f"(new_call_sid={new_call_sid})"
        )

    # ------------------------------------------------------------------
    # Release
    # ------------------------------------------------------------------

    async def release_worker(self, call_sid: str) -> None:
        """Release the worker assigned to call_sid.

        Redis path:
          1. Look up worker_id via invorto:worker:call:{call_sid} with retry.
             The retry covers RC-8: status webhook arriving in the ~50 ms window
             between Twilio API return and reassign_call_sid writing the key.
          2. Atomically clear the worker HASH and delete the call key.
          3. Update local cache.

        If the call key is still absent after retries (genuinely missing), falls
        back to a local pool scan so a runner restart does not strand workers.
        """
        _worker_to_cancel = None

        if self._redis:
            worker_id: Optional[str] = None

            for attempt in range(self._RELEASE_LOOKUP_RETRIES):
                try:
                    worker_id = await self._redis.get_worker_for_call(call_sid)
                except Exception as e:
                    logger.warning(
                        f"Redis lookup error in release_worker for {call_sid} "
                        f"(attempt {attempt + 1}): {e}"
                    )
                if worker_id:
                    break
                if attempt < self._RELEASE_LOOKUP_RETRIES - 1:
                    await asyncio.sleep(self._RELEASE_LOOKUP_DELAY)

            if worker_id:
                try:
                    await self._redis.release_assignment(worker_id, call_sid)
                except Exception as e:
                    logger.error(
                        f"Redis error releasing {worker_id} for call {call_sid}: {e}"
                    )

                async with self._lock:
                    worker = self.workers.get(worker_id)
                    if worker:
                        worker.current_call_sid = None
                        worker.assigned_at = None
                        _worker_to_cancel = worker

                logger.info(f"Released worker {worker_id} for call {call_sid}")
            else:
                # Redis has no mapping — scan local pool as fallback
                logger.warning(
                    f"No Redis call mapping for {call_sid} after "
                    f"{self._RELEASE_LOOKUP_RETRIES} attempts — scanning local pool."
                )
                async with self._lock:
                    for worker in self.workers.values():
                        if worker.current_call_sid == call_sid:
                            worker.current_call_sid = None
                            worker.assigned_at = None
                            _worker_to_cancel = worker
                            logger.info(
                                f"Released worker {worker.instance_id} for call "
                                f"{call_sid} (local fallback)"
                            )
                            break
                    else:
                        logger.warning(f"No worker found for call {call_sid}")
        else:
            async with self._lock:
                for worker in self.workers.values():
                    if worker.current_call_sid == call_sid:
                        worker.current_call_sid = None
                        worker.assigned_at = None
                        _worker_to_cancel = worker
                        logger.info(
                            f"Released worker {worker.instance_id} for call {call_sid}"
                        )
                        break
                else:
                    logger.warning(f"No worker found for call {call_sid} to release")

        if _worker_to_cancel is not None:
            asyncio.create_task(self._cancel_prewarm(_worker_to_cancel, call_sid))

    async def release_worker_by_id(self, worker_id: str) -> bool:
        """Manually release a worker by its instance_id (admin/drain endpoint)."""
        async with self._lock:
            worker = self.workers.get(worker_id)
            if not worker:
                logger.warning(f"Worker not found: {worker_id}")
                return False
            call_sid = worker.current_call_sid

        if self._redis:
            try:
                if call_sid:
                    await self._redis.release_assignment(worker_id, call_sid)
                else:
                    await self._redis.clear_worker_state(worker_id)
            except Exception as e:
                logger.error(
                    f"Redis error in release_worker_by_id for {worker_id}: {e}"
                )

        _worker_to_cancel = None
        _call_sid_to_cancel = None
        async with self._lock:
            worker = self.workers.get(worker_id)
            if not worker:
                return True
            worker.current_call_sid = None
            worker.assigned_at = None
            logger.info(f"Manually released worker {worker_id} (call_sid={call_sid})")
            if call_sid:
                _worker_to_cancel = worker
                _call_sid_to_cancel = call_sid

        if _worker_to_cancel is not None:
            asyncio.create_task(
                self._cancel_prewarm(_worker_to_cancel, _call_sid_to_cancel)
            )
        return True

    # ------------------------------------------------------------------
    # Worker state for API response
    # ------------------------------------------------------------------

    async def get_all_workers_state(self) -> list[dict]:
        """Return worker state enriched with live Redis assignment data.

        Reads Redis in a single pipeline round-trip (one HGETALL per worker).
        Falls back to local cache if Redis is unavailable or not configured.
        """
        async with self._lock:
            snapshot = list(self.workers.values())

        redis_states: dict[str, dict] = {}
        if self._redis and snapshot:
            try:
                worker_ids = [w.instance_id for w in snapshot]
                redis_states = await self._redis.batch_get_states(worker_ids)
            except Exception as e:
                logger.warning(
                    f"Redis batch_get_states failed in get_all_workers_state: {e} — "
                    f"falling back to local cache."
                )

        result = []
        for w in snapshot:
            state = redis_states.get(w.instance_id, {})
            # Redis is authoritative; fall back to local cache if Redis unavailable.
            current_call_sid = state.get("current_call_sid") or w.current_call_sid or None
            assigned_at_raw = state.get("assigned_at") or (
                str(w.assigned_at) if w.assigned_at else None
            )
            try:
                assigned_at = float(assigned_at_raw) if assigned_at_raw else None
            except (ValueError, TypeError):
                assigned_at = w.assigned_at

            result.append(
                {
                    "worker_id": w.instance_id,
                    "host": w.host,
                    "is_available": w.consecutive_failures < 3 and not current_call_sid,
                    "current_call_sid": current_call_sid,
                    "assigned_at": assigned_at,
                    "last_health_check": w.last_health_check,
                    "consecutive_failures": w.consecutive_failures,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Prewarm helpers
    # ------------------------------------------------------------------

    def _get_worker_url(self, worker: WorkerStatus, path: str) -> str:
        if worker.private_ip:
            return f"http://{worker.private_ip}:{WORKER_PORT}{path}"
        return f"https://{worker.host}{path}"

    async def _send_prewarm(
        self, worker: WorkerStatus, call_sid: str, config_payload: Optional[dict] = None
    ) -> None:
        try:
            url = self._get_worker_url(worker, "/prewarm")
            headers = {}
            if API_KEY:
                headers["X-API-Key"] = API_KEY
            if WORKER_AUTH_TOKEN:
                headers["X-Worker-Auth"] = WORKER_AUTH_TOKEN
            body: dict = {"call_sid": call_sid}
            if config_payload:
                body.update(config_payload)
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json=body, headers=headers)
            logger.debug(
                f"[prewarm] triggered call_sid={call_sid} worker={worker.instance_id}"
            )
        except Exception as e:
            logger.debug(f"[prewarm] trigger failed call_sid={call_sid}: {e}")

    async def send_prewarm_and_wait(
        self,
        worker: WorkerStatus,
        call_sid: str,
        config_payload: dict,
        timeout: float = 5.0,
    ) -> bool:
        try:
            url = self._get_worker_url(worker, "/prewarm")
            headers = {}
            if API_KEY:
                headers["X-API-Key"] = API_KEY
            if WORKER_AUTH_TOKEN:
                headers["X-Worker-Auth"] = WORKER_AUTH_TOKEN
            body = {"call_sid": call_sid, "wait": True, **config_payload}
            async with httpx.AsyncClient(timeout=timeout + 1.0) as client:
                response = await client.post(url, json=body, headers=headers)
            data = response.json()
            ready = data.get("status") == "ready"
            logger.info(
                f"[prewarm] call_sid={call_sid} worker={worker.instance_id} "
                f"status={data.get('status')} ready={ready}"
            )
            return ready
        except Exception as e:
            logger.warning(
                f"[prewarm] send_prewarm_and_wait failed call_sid={call_sid}: {e}"
            )
            return False

    async def _send_prewarm_reassign(
        self, worker: WorkerStatus, old_key: str, new_key: str
    ) -> None:
        try:
            url = self._get_worker_url(worker, "/prewarm/reassign")
            headers = {}
            if API_KEY:
                headers["X-API-Key"] = API_KEY
            if WORKER_AUTH_TOKEN:
                headers["X-Worker-Auth"] = WORKER_AUTH_TOKEN
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    url, json={"old_key": old_key, "new_key": new_key}, headers=headers
                )
            logger.debug(
                f"[prewarm] reassigned {old_key} → {new_key} worker={worker.instance_id}"
            )
        except Exception as e:
            logger.debug(f"[prewarm] reassign failed {old_key} → {new_key}: {e}")

    async def _cancel_prewarm(self, worker: WorkerStatus, call_sid: str) -> None:
        try:
            url = self._get_worker_url(worker, f"/prewarm/{call_sid}")
            headers = {}
            if API_KEY:
                headers["X-API-Key"] = API_KEY
            if WORKER_AUTH_TOKEN:
                headers["X-Worker-Auth"] = WORKER_AUTH_TOKEN
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.delete(url, headers=headers)
            logger.debug(
                f"[prewarm] cancelled call_sid={call_sid} worker={worker.instance_id}"
            )
        except Exception as e:
            logger.debug(f"[prewarm] cancel failed call_sid={call_sid}: {e}")

    def trigger_prewarm_nowait(
        self, worker: WorkerStatus, call_sid: str, config_payload: Optional[dict] = None
    ) -> None:
        asyncio.create_task(self._send_prewarm(worker, call_sid, config_payload))

    def trigger_prewarm_reassign_nowait(
        self, worker: WorkerStatus, old_key: str, new_key: str
    ) -> None:
        asyncio.create_task(self._send_prewarm_reassign(worker, old_key, new_key))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._health_check_task is None:
            self._health_check_task = asyncio.create_task(self.run_health_checks())
            logger.info("Started worker pool health checks")

    async def stop(self) -> None:
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
            self._health_check_task = None
        await self._teardown()
        if self._redis:
            await self._redis.close()
