from fastapi import APIRouter, Request, Depends, HTTPException

from ..auth.session import require_admin
from ..queue.ordering import refund_item

router = APIRouter(prefix="/admin/queue", tags=["admin"])


@router.post("/clear")
async def clear_queue(request: Request, user: dict = Depends(require_admin)):
    """Clear the CyTube playlist (refunds all pay items)."""
    db = request.app.state.db
    api_gate = request.app.state.api_gate
    shadow = request.app.state.shadow

    # Refund all pay items
    pay_items = await db.get_pay_items()
    for item in pay_items:
        await refund_item(api_gate=api_gate, db=db, uid=item["uid"], reason="admin_clear")

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
