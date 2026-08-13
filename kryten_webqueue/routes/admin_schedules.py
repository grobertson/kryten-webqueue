from fastapi import APIRouter, Request, Depends, HTTPException
from datetime import datetime, UTC

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
async def get_schedule(
    request: Request, schedule_id: int, user: dict = Depends(require_admin)
):
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
async def update_schedule(
    request: Request, schedule_id: int, user: dict = Depends(require_admin)
):
    """Update a schedule.

    Always re-registers the APScheduler job from the authoritative DB value
    after the update, regardless of which fields changed. This ensures that
    modifications made close to the fire time (including playlist-only changes)
    take effect for the immediate scheduled item.
    """
    body = await request.json()
    db = request.app.state.db
    scheduler = request.app.state.scheduler

    sched = await db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(404, "Schedule not found")

    await db.update_schedule(schedule_id, **body)

    # Re-read from DB so the scheduler always uses the authoritative fire_at
    # (not the body value, which may differ due to serialization or race with
    # a recurring re-arm). Only arm a future job; past schedules won't re-fire.
    updated = await db.get_schedule(schedule_id)
    await scheduler.remove_schedule(schedule_id)
    if updated and updated.get("is_active"):
        fire_at = scheduler._parse_fire_at(updated["fire_at"])
        if fire_at > datetime.now(UTC):
            await scheduler.add_schedule(schedule_id, fire_at)

    return {"success": True}


@router.delete("/{schedule_id}")
async def delete_schedule(
    request: Request, schedule_id: int, user: dict = Depends(require_admin)
):
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
async def fire_now(
    request: Request, schedule_id: int, user: dict = Depends(require_admin)
):
    """Manually fire a schedule immediately."""
    from ..playlists.fire import fire_schedule

    db = request.app.state.db
    sched = await db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(404, "Schedule not found")

    config = request.app.state.config
    await fire_schedule(
        schedule_id=schedule_id,
        api_gate=request.app.state.api_gate,
        db=db,
        shadow=request.app.state.shadow,
        ws_manager=request.app.state.ws_manager,
        add_delay_sec=config.playlist_bulk_add_delay_sec,
        add_max_retries=config.playlist_bulk_add_max_retries,
        promo_director=getattr(request.app.state, "promo_director", None),
    )
    return {"success": True}


@router.post("/clear-active")
async def clear_active(request: Request, user: dict = Depends(require_admin)):
    """Clear the active schedule (return to free mode)."""
    db = request.app.state.db
    await db.clear_active_schedule()
    return {"success": True}


@router.get("/lock-status")
async def lock_status(request: Request, user: dict = Depends(require_admin)):
    """Authoritative pay-to-play lock state for the admin lock banner.

    Reports *both* lock types so an admin always sees why the queue is closed
    and can end it — whether a schedule is in its pre-fire window (no active
    schedule row) or an immutable event is mid-play.
    """
    db = request.app.state.db

    # Pre-fire window lives entirely in playlist_schedules (no active_schedule
    # row), which is why "Clear Active" can't see or clear it.
    if await db.is_pre_fire_lock_active():
        lock = await db.get_active_pre_fire_lock() or {}
        return {
            "locked": True,
            "type": "pre_fire",
            "label": lock.get("label"),
            "fire_at": lock.get("fire_at"),
        }

    # In-progress immutable scheduled event.
    if await db.is_event_lock_active():
        active = await db.get_active_schedule() or {}
        label = None
        if active.get("playlist_id"):
            playlist = await db.get_saved_playlist(active["playlist_id"])
            if playlist:
                label = playlist.get("name")
        return {
            "locked": True,
            "type": "event",
            "label": label,
            "estimated_end_at": active.get("estimated_end_at"),
        }

    return {"locked": False, "type": None}


@router.post("/unlock")
async def unlock(request: Request, user: dict = Depends(require_admin)):
    """End the active pay-to-play lockout, whatever its source.

    Lifts an in-progress event lock *and* every currently-active pre-fire lock
    in a single action, so one click reliably reopens pay-to-play. The schedules
    themselves stay armed: a recurring schedule re-locks on its next firing.
    """
    db = request.app.state.db
    lifted: list[str] = []

    if await db.is_event_lock_active():
        await db.disable_active_lock()
        lifted.append("event")

    pre_fire_count = await db.disable_active_pre_fire_locks()
    if pre_fire_count:
        lifted.append("pre_fire")

    return {"success": True, "lifted": lifted, "pre_fire_count": pre_fire_count}
