from fastapi import APIRouter, Request, Depends, HTTPException

from ..auth.session import require_admin

router = APIRouter(prefix="/admin/jobs", tags=["admin"])


@router.get("")
async def list_jobs(request: Request, user: dict = Depends(require_admin)):
    """List registered background jobs and whether each is running."""
    job_manager = request.app.state.job_manager
    return job_manager.list_jobs()


@router.get("/runs")
async def job_runs(request: Request, user: dict = Depends(require_admin), job: str | None = None, limit: int = 10):
    """Recent run history, optionally filtered by job name."""
    db = request.app.state.db
    return await db.get_job_runs(job_name=job, limit=limit)


@router.post("/{name}/run")
async def run_job(request: Request, name: str, user: dict = Depends(require_admin)):
    """Trigger a registered job to run in the background."""
    job_manager = request.app.state.job_manager
    try:
        result = await job_manager.run(name, triggered_by=user["username"])
    except KeyError:
        raise HTTPException(404, f"Unknown job: {name}")
    if not result.get("started"):
        raise HTTPException(409, result.get("reason", "Job already running"))
    return {"success": True, **result}
