import asyncio
from typing import Optional

from loguru import logger
from pipecat.pipeline.task import PipelineTask


class WorkerState:
    def __init__(self):
        self.is_available = True
        self.current_call_sid: Optional[str] = None
        self.active_task: Optional[PipelineTask] = None
        self._lock = asyncio.Lock()

    async def start_call(self, call_sid: str):
        async with self._lock:
            self.is_available = False
            self.current_call_sid = call_sid
            logger.info(f"Started handling call: {call_sid}")

    async def end_call(self):
        async with self._lock:
            call_sid = self.current_call_sid
            self.is_available = True
            self.current_call_sid = None
            self.active_task = None
            logger.info(f"Ended call: {call_sid}")

    async def get_health_snapshot(self) -> dict:
        """Return a lock-consistent availability snapshot for health checks."""
        async with self._lock:
            return {
                "available": self.is_available,
                "current_call": self.current_call_sid,
            }


worker_state = WorkerState()
