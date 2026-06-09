from fastapi import APIRouter, Request, Depends, HTTPException

from ..auth.session import require_admin

router = APIRouter(prefix="/admin/jobs", tags=["admin"])


@router.get("")
async def list_jobs(request: Request, user: dict = Depends(require_admin)):
    """List registered jobs with their parameter schema and last-run summary."""
    job_manager = request.app.state.job_manager
    db = request.app.state.db
    jobs = job_manager.list_jobs()
    for job in jobs:
        runs = await db.get_job_runs(job_name=job["name"], limit=1)
        job["last_run"] = runs[0] if runs else None
    return jobs


@router.get("/runs")
async def job_runs(request: Request, user: dict = Depends(require_admin), job: str | None = None, limit: int = 10):
    """Recent run history, optionally filtered by job name."""
    db = request.app.state.db
    return await db.get_job_runs(job_name=job, limit=limit)


@router.get("/{name}/schema")
async def job_schema(request: Request, name: str, user: dict = Depends(require_admin)):
    """Return a job's parameter schema for rendering the Run form."""
    job_manager = request.app.state.job_manager
    try:
        return {"name": name, "schema": job_manager.get_schema(name)}
    except KeyError:
        raise HTTPException(404, f"Unknown job: {name}")


@router.post("/{name}/run")
async def run_job(request: Request, name: str, user: dict = Depends(require_admin)):
    """Trigger a registered job to run in the background.

    Accepts an optional JSON body ``{"params": {...}}`` validated against the
    job's schema. Returns 400 on invalid params, 404 for an unknown job, and
    409 if the job is already running.
    """
    job_manager = request.app.state.job_manager
    params = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            params = body.get("params") if "params" in body else (body or None)
    except Exception:
        params = None  # empty/non-JSON body → run with no params
    try:
        result = await job_manager.run(name, triggered_by=user["username"], params=params)
    except KeyError:
        raise HTTPException(404, f"Unknown job: {name}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not result.get("started"):
        raise HTTPException(409, result.get("reason", "Job already running"))
    return {"success": True, **result}
