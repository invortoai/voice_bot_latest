from fastapi import APIRouter, HTTPException

from app.services.worker_pool import worker_pool

router = APIRouter(tags=["Workers"])


@router.get("/workers")
async def list_workers():
    """Return all workers with live assignment state read directly from Redis."""
    states = await worker_pool.get_all_workers_state()
    return {
        "workers": states,
        "total": len(states),
        "available": sum(1 for w in states if w.get("is_available")),
    }


@router.post("/workers/refresh")
async def refresh_workers():
    await worker_pool.discover_workers()
    return {"status": "refreshed", "worker_count": len(worker_pool.workers)}


@router.post("/workers/{worker_id}/release")
async def release_worker_manually(worker_id: str):
    released = await worker_pool.release_worker_by_id(worker_id)
    if released:
        return {"status": "released", "worker_id": worker_id}
    raise HTTPException(status_code=404, detail="Worker not found")
