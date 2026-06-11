from fastapi import APIRouter, Request, Depends, HTTPException
from datetime import datetime

from ..auth.session import require_admin

router = APIRouter(prefix="/admin/schedules", tags=["admin"])


@router.get("/")
async def list_schedules(request: Request, user: dict = Depends(require_admin)):
    """List all playlist schedules."""
    db = request.app.state.db
    return await db.get_schedules()


@router.get("/active")
async def get_active(request: Request, user: dict = Depends(require_admin)):
    """Get the currently active schedule."""
    db = request.app.state.db
    active = await db.get_active_schedule()
    return active or {}


@router.get("/next")
async def get_next(request: Request, user: dict = Depends(require_admin)):
    """Get the next upcoming schedule."""
    db = request.app.state.db
    next_sched = await db.get_next_schedule()
    return next_sched or {}


@router.get("/{schedule_id}")
async def get_schedule(request: Request, schedule_id: int, user: dict = Depends(require_admin)):
    """Get a specific schedule."""
    db = request.app.state.db
    sched = await db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(404, "Schedule not found")
    return sched


@router.post("/")
async def create_schedule(request: Request, user: dict = Depends(require_admin)):
    """Create a new schedule."""
    body = await request.json()
    db = request.app.state.db
    scheduler = request.app.state.scheduler

    schedule_id = await db.create_schedule(
        playlist_id=body["playlist_id"],
        label=body["label"],
        fire_at=body["fire_at"],
        is_recurring=body.get("is_recurring", False),
        rrule=body.get("rrule"),
        pre_fire_lock_minutes=body.get("pre_fire_lock_minutes", 15),
        fallback_playlist_id=body.get("fallback_playlist_id"),
        is_active=True,
        created_by=user["username"],
    )

    # Register with APScheduler
    fire_at = datetime.fromisoformat(body["fire_at"])
    await scheduler.add_schedule(schedule_id, fire_at)

    return {"id": schedule_id}


@router.put("/{schedule_id}")
async def update_schedule(request: Request, schedule_id: int, user: dict = Depends(require_admin)):
    """Update a schedule."""
    body = await request.json()
    db = request.app.state.db
    scheduler = request.app.state.scheduler

    sched = await db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(404, "Schedule not found")

    await db.update_schedule(schedule_id, **body)

    # Re-register if fire_at changed
    if "fire_at" in body:
        await scheduler.remove_schedule(schedule_id)
        fire_at = datetime.fromisoformat(body["fire_at"])
        await scheduler.add_schedule(schedule_id, fire_at)

    return {"success": True}


@router.delete("/{schedule_id}")
async def delete_schedule(request: Request, schedule_id: int, user: dict = Depends(require_admin)):
    """Delete a schedule."""
    db = request.app.state.db
    scheduler = request.app.state.scheduler

    sched = await db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(404, "Schedule not found")

    await scheduler.remove_schedule(schedule_id)
    await db.delete_schedule(schedule_id)
    return {"success": True}


@router.post("/{schedule_id}/fire")
async def fire_now(request: Request, schedule_id: int, user: dict = Depends(require_admin)):
    """Manually fire a schedule immediately."""
    from ..playlists.fire import fire_schedule

    db = request.app.state.db
    sched = await db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(404, "Schedule not found")

    await fire_schedule(
        schedule_id=schedule_id,
        api_gate=request.app.state.api_gate,
        db=db,
        shadow=request.app.state.shadow,
        ws_manager=request.app.state.ws_manager,
    )
    return {"success": True}


@router.post("/clear-active")
async def clear_active(request: Request, user: dict = Depends(require_admin)):
    """Clear the active schedule (return to free mode)."""
    db = request.app.state.db
    await db.clear_active_schedule()
    return {"success": True}


@router.post("/unlock")
async def unlock(request: Request, user: dict = Depends(require_admin)):
    """Lift the currently-active pay-to-play lock without deleting the schedule.

    Targets the in-progress scheduled-event lock first (keeps the event banner
    and any recurring schedule armed); otherwise lifts an active pre-fire lock
    for its current occurrence only (a recurring schedule re-locks on its next
    firing).
    """
    db = request.app.state.db

    if await db.is_event_lock_active():
        await db.disable_active_lock()
        return {"success": True, "lifted": "event"}

    prefire = await db.get_active_pre_fire_lock()
    if prefire:
        await db.update_schedule(prefire["id"], lock_disabled=1)
        return {"success": True, "lifted": "pre_fire"}

    return {"success": True, "lifted": None}
