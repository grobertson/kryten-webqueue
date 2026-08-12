from fastapi import APIRouter, Request, Depends, HTTPException

from ..auth.session import require_admin

router = APIRouter(prefix="/admin/job-schedules", tags=["admin"])


@router.get("")
async def list_job_schedules(request: Request, user: dict = Depends(require_admin)):
    """List all persisted job schedules."""
    return await request.app.state.db.get_job_schedules()


@router.get("/{job_name}")
async def get_job_schedule(
    request: Request, job_name: str, user: dict = Depends(require_admin)
):
    """Return the schedule for a specific job, or 404 if none exists."""
    sched = await request.app.state.db.get_job_schedule(job_name)
    if not sched:
        raise HTTPException(404, f"No schedule for job: {job_name}")
    return sched


@router.post("")
async def upsert_job_schedule(request: Request, user: dict = Depends(require_admin)):
    """Create or update a job schedule.

    Body: {job_name, cron_expression, params?, label?, run_next_job?, is_active?}
    cron_expression must be a standard 5-field cron string (min hour dom mon dow).
    """
    body = await request.json()
    job_name = (body.get("job_name") or "").strip()
    cron_expression = (body.get("cron_expression") or "").strip()
    if not job_name:
        raise HTTPException(400, "job_name is required")
    if not cron_expression:
        raise HTTPException(400, "cron_expression is required")
    if len(cron_expression.split()) != 5:
        raise HTTPException(400, "cron_expression must have exactly 5 fields (min hour dom mon dow)")

    job_scheduler = request.app.state.job_scheduler
    await job_scheduler.upsert(
        job_name,
        cron_expression,
        params=body.get("params") or None,
        label=body.get("label") or None,
        run_next_job=body.get("run_next_job") or None,
        is_active=bool(body.get("is_active", True)),
        created_by=user["username"],
    )
    return {"success": True}


@router.put("/{job_name}")
async def update_job_schedule(
    request: Request, job_name: str, user: dict = Depends(require_admin)
):
    """Update an existing job schedule (same contract as POST)."""
    body = await request.json()
    cron_expression = (body.get("cron_expression") or "").strip()
    if not cron_expression:
        raise HTTPException(400, "cron_expression is required")
    if len(cron_expression.split()) != 5:
        raise HTTPException(400, "cron_expression must have exactly 5 fields (min hour dom mon dow)")

    job_scheduler = request.app.state.job_scheduler
    await job_scheduler.upsert(
        job_name,
        cron_expression,
        params=body.get("params") or None,
        label=body.get("label") or None,
        run_next_job=body.get("run_next_job") or None,
        is_active=bool(body.get("is_active", True)),
        created_by=user["username"],
    )
    return {"success": True}


@router.delete("/{job_name}")
async def delete_job_schedule(
    request: Request, job_name: str, user: dict = Depends(require_admin)
):
    """Remove a job schedule."""
    job_scheduler = request.app.state.job_scheduler
    await job_scheduler.remove(job_name)
    return {"success": True}
