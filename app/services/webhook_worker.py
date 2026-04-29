"""
Background webhook delivery worker.

Polls webhook_deliveries for pending/failed rows and delivers them.
Started in the runner lifespan, stopped on shutdown.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from app.config import (
    WEBHOOK_ENABLED,
    WEBHOOK_POLL_INTERVAL_SECONDS,
)
from app.core.database import get_cursor
from app.services.webhook_service import deliver_webhook

# Max rows per poll cycle (prevents long-running batches)
_BATCH_SIZE = 20


class WebhookWorker:
    """Async background worker that polls and delivers pending webhooks."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if not WEBHOOK_ENABLED:
            logger.info("webhook_worker: disabled via WEBHOOK_ENABLED=false")
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"webhook_worker: started (poll_interval={WEBHOOK_POLL_INTERVAL_SECONDS}s)"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("webhook_worker: stopped")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._process_batch()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"webhook_worker: poll error: {exc}")

            try:
                await asyncio.sleep(WEBHOOK_POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break

    async def _process_batch(self) -> None:
        """Claim and deliver a batch of due webhooks."""
        rows = _claim_pending_deliveries(_BATCH_SIZE)
        if not rows:
            return

        logger.info(f"webhook_worker: processing {len(rows)} deliveries")

        for row in rows:
            delivery_id = str(row["id"])
            try:
                await deliver_webhook(delivery_id)
            except Exception as exc:
                logger.error(f"webhook_worker: delivery {delivery_id} error: {exc}")


def _claim_pending_deliveries(limit: int) -> list[dict]:
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id, call_request_id, webhook_url, attempt_number
                FROM webhook_deliveries
                WHERE (
                    (status = 'failed' AND next_retry_at <= NOW())
                    OR
                    (status = 'pending' AND (next_retry_at IS NULL OR next_retry_at <= NOW()))
                )
                ORDER BY created_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        logger.error(f"webhook_worker: claim query failed: {exc}")
        return []
