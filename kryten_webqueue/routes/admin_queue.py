from fastapi import APIRouter, Request, Depends, HTTPException

from ..auth.session import require_admin
from ..queue.ordering import refund_item, insert_admin_queue

router = APIRouter(prefix="/admin/queue", tags=["admin"])


@router.post("/add")
async def admin_add(request: Request, user: dict = Depends(require_admin)):
    """Queue an item as admin: zero cost, position resolved by `mode`."""
    body = await request.json()
    friendly_token = body.get("friendly_token")
    mode = body.get("mode", "after_purchased")
    if not friendly_token:
        raise HTTPException(400, "friendly_token required")
    if mode not in ("after_purchased", "playnext_refund", "cancel"):
        raise HTTPException(400, "invalid mode")
    if mode == "cancel":
        return {"success": False, "cancelled": True}

    db = request.app.state.db
    api_gate = request.app.state.api_gate
    shadow = request.app.state.shadow

    # Check pre-fire lock
    if await db.is_pre_fire_lock_active():
        raise HTTPException(423, "Queue is locked: scheduled playlist firing soon")

    # Admins bypass catalog visibility filters (reserved/blackout) — they are
    # in control of the catalog and may queue any item manually.
    item = await db.get_item_admin(friendly_token)
    if not item:
        raise HTTPException(404, "Item not found in catalog")

    result = await insert_admin_queue(
        api_gate=api_gate,
        shadow=shadow,
        db=db,
        username=user["username"],
        media_type="cm",
        media_id=item["manifest_url"],
        friendly_token=friendly_token,
        title=item["title"],
        duration_sec=item["duration_sec"],
        mode=mode,
    )

    if not result["success"]:
        raise HTTPException(400, result.get("error", "Admin queue add failed"))
    return result


@router.post("/clear")
async def clear_queue(request: Request, user: dict = Depends(require_admin)):
    """Clear the CyTube playlist (refunds all pay items)."""
    db = request.app.state.db
    api_gate = request.app.state.api_gate
    shadow = request.app.state.shadow

    # Refund all pay items
    pay_items = await db.get_pay_items()
    for item in pay_items:
        await refund_item(
            api_gate=api_gate, db=db, uid=item["uid"], reason="admin_clear"
        )

    # Clear playlist
    await api_gate.playlist_clear()
    return {"success": True, "refunded": len(pay_items)}


@router.delete("/{uid}")
async def remove_item(request: Request, uid: int, user: dict = Depends(require_admin)):
    """Remove an item from queue (with refund if paid)."""
    db = request.app.state.db
    api_gate = request.app.state.api_gate
    shadow = request.app.state.shadow

    # Try refund
    await refund_item(api_gate=api_gate, db=db, uid=uid, reason="admin_remove")

    # Remove from CyTube
    await api_gate.playlist_delete(uid)
    await shadow.remove(uid)
    return {"success": True}


@router.post("/{uid}/jump")
async def jump_to(request: Request, uid: int, user: dict = Depends(require_admin)):
    """Jump to a specific item in the playlist."""
    api_gate = request.app.state.api_gate
    await api_gate.playlist_jump(uid)
    return {"success": True}


@router.get("/sync-logs")
async def get_sync_logs(request: Request, user: dict = Depends(require_admin)):
    """Get recent catalog sync logs."""
    db = request.app.state.db
    return await db.get_sync_logs()


@router.post("/sync-now")
async def trigger_sync(request: Request, user: dict = Depends(require_admin)):
    """Trigger an immediate catalog sync."""
    catalog_sync = request.app.state.catalog_sync
    # Run in background to not block response
    import asyncio

    asyncio.create_task(catalog_sync.sync())
    return {"success": True, "message": "Sync started"}
