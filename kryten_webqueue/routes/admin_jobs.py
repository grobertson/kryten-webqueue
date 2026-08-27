import json

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import Response

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
async def job_runs(
    request: Request,
    user: dict = Depends(require_admin),
    job: str | None = None,
    limit: int = 10,
):
    """Recent run history, optionally filtered by job name."""
    db = request.app.state.db
    return await db.get_job_runs(job_name=job, limit=limit)


@router.get("/runs/{run_id}/log")
async def job_run_log(
    request: Request, run_id: int, user: dict = Depends(require_admin)
):
    """Return the captured full-text log lines for a single run."""
    db = request.app.state.db
    run = await db.get_job_run(run_id)
    if not run:
        raise HTTPException(404, "Unknown run")
    return await db.get_job_run_logs(run_id)


@router.get("/runs/{run_id}/log/download")
async def download_job_run_log(
    request: Request, run_id: int, user: dict = Depends(require_admin)
):
    """Download the run's full-text log as a plain-text file."""
    db = request.app.state.db
    run = await db.get_job_run(run_id)
    if not run:
        raise HTTPException(404, "Unknown run")
    lines = await db.get_job_run_logs(run_id)
    body = "\n".join(
        f"{ln['logged_at']} {ln.get('level', ''):<8} {ln.get('logger', '')}: {ln['message']}"
        for ln in lines
    )
    filename = f"{run['job_name']}-run{run_id}.log"
    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/runs/{run_id}/detail/download")
async def download_job_run_detail(
    request: Request, run_id: int, user: dict = Depends(require_admin)
):
    """Download the run's JSON summary (the ``detail`` blob) as a file."""
    db = request.app.state.db
    run = await db.get_job_run(run_id)
    if not run:
        raise HTTPException(404, "Unknown run")
    detail = run.get("detail")
    try:
        body = json.dumps(json.loads(detail), indent=2) if detail else "{}"
    except (TypeError, ValueError):
        body = detail or "{}"
    filename = f"{run['job_name']}-run{run_id}.json"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
        result = await job_manager.run(
            name, triggered_by=user["username"], params=params
        )
    except KeyError:
        raise HTTPException(404, f"Unknown job: {name}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not result.get("started"):
        raise HTTPException(409, result.get("reason", "Job already running"))
    return {"success": True, **result}


@router.get("/fetch-queue")
async def get_fetch_queue(
    request: Request,
    user: dict = Depends(require_admin),
    page: int = 1,
    limit: int = 20,
):
    """Return a page of the download queue (newest first).

    Successful downloads finished more than 24h ago are hidden from the list to
    keep it manageable; failed / pending / running items always remain visible.
    Rows are never deleted — this is a visibility filter only, so the full audit
    trail is preserved. Paginated at ``limit`` items per page (default 20).
    """
    db = request.app.state.db
    page = max(1, page)
    limit = max(1, min(limit, 100))
    offset = (page - 1) * limit
    total = await db.count_fetch_queue(hide_expired_done=True)
    items = await db.get_fetch_queue(limit=limit, offset=offset, hide_expired_done=True)
    pages = max(1, (total + limit - 1) // limit)
    return {
        "items": items,
        "total": total,
        "page": page,
        "pages": pages,
        "limit": limit,
    }


@router.delete("/fetch-queue/{item_id}")
async def delete_fetch_queue_item(
    request: Request, item_id: int, user: dict = Depends(require_admin)
):
    """Remove a pending or finished fetch queue item (running items are protected)."""
    db = request.app.state.db
    ok = await db.delete_fetch_queue_item(item_id)
    if not ok:
        raise HTTPException(404, "Item not found or currently running")
    return {"ok": True}
