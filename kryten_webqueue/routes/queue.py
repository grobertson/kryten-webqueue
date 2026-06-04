from fastapi import APIRouter, Request, Depends, HTTPException

from ..auth.session import get_current_user
from ..queue.ordering import insert_pay_queue, insert_pay_playnext

router = APIRouter(prefix="/queue", tags=["queue"])


@router.get("/state")
async def get_queue_state(request: Request, user: dict = Depends(get_current_user)):
    """Get current queue state."""
    shadow = request.app.state.shadow
    return shadow.get_queue_state()


@router.post("/add")
async def add_to_queue(request: Request, user: dict = Depends(get_current_user)):
    """Add an item to the pay queue (FIFO)."""
    body = await request.json()
    friendly_token = body.get("friendly_token")
    tier = body.get("tier", "queue")

    if not friendly_token:
        raise HTTPException(400, "friendly_token required")

    db = request.app.state.db
    api_gate = request.app.state.api_gate
    shadow = request.app.state.shadow

    # Check pre-fire lock
    if await db.is_pre_fire_lock_active():
        raise HTTPException(423, "Queue is locked: scheduled playlist firing soon")

    # Look up catalog item
    item = await db.get_item(friendly_token)
    if not item:
        raise HTTPException(404, "Item not found in catalog")

    # Preview cost (api-gate _unwrap strips the success envelope; raise_for_status
    # handles non-2xx, so no success check needed here)
    preview = await api_gate.queue_preview(
        username=user["username"],
        duration_sec=item["duration_sec"],
        tier=tier,
    )
    if not preview.get("available", True):
        error_code = preview.get("error_code") or "unavailable"
        raise HTTPException(400, error_code)
    z_cost = preview.get("cost_z")
    if z_cost is None:
        raise HTTPException(502, "Cost preview returned no cost value")

    result = await insert_pay_queue(
        api_gate=api_gate,
        shadow=shadow,
        db=db,
        username=user["username"],
        media_type="cm",
        media_id=friendly_token,
        title=item["title"],
        duration_sec=item["duration_sec"],
        tier=tier,
        z_cost=z_cost,
    )

    if not result["success"]:
        raise HTTPException(400, result.get("error", "Queue add failed"))
    return result


@router.post("/playnext")
async def play_next(request: Request, user: dict = Depends(get_current_user)):
    """Add item as play-next (premium tier)."""
    body = await request.json()
    friendly_token = body.get("friendly_token")
    tier = "playnext"

    if not friendly_token:
        raise HTTPException(400, "friendly_token required")

    db = request.app.state.db
    api_gate = request.app.state.api_gate
    shadow = request.app.state.shadow

    # Check pre-fire lock
    if await db.is_pre_fire_lock_active():
        raise HTTPException(423, "Queue is locked: scheduled playlist firing soon")

    # Look up catalog item
    item = await db.get_item(friendly_token)
    if not item:
        raise HTTPException(404, "Item not found in catalog")

    # Preview cost (api-gate _unwrap strips the success envelope; raise_for_status
    # handles non-2xx, so no success check needed here)
    preview = await api_gate.queue_preview(
        username=user["username"],
        duration_sec=item["duration_sec"],
        tier=tier,
    )
    if not preview.get("available", True):
        error_code = preview.get("error_code") or "unavailable"
        raise HTTPException(400, error_code)
    z_cost = preview.get("cost_z")
    if z_cost is None:
        raise HTTPException(502, "Cost preview returned no cost value")

    result = await insert_pay_playnext(
        api_gate=api_gate,
        shadow=shadow,
        db=db,
        username=user["username"],
        media_type="cm",
        media_id=friendly_token,
        title=item["title"],
        duration_sec=item["duration_sec"],
        tier=tier,
        z_cost=z_cost,
    )

    if not result["success"]:
        raise HTTPException(400, result.get("error", "Playnext failed"))
    return result


@router.get("/preview")
async def cost_preview(request: Request, friendly_token: str, tier: str = "queue",
                       user: dict = Depends(get_current_user)):
    """Preview cost for queuing an item."""
    db = request.app.state.db
    api_gate = request.app.state.api_gate

    item = await db.get_item(friendly_token)
    if not item:
        raise HTTPException(404, "Item not found")

    preview = await api_gate.queue_preview(
        username=user["username"],
        duration_sec=item["duration_sec"],
        tier=tier,
    )
    return preview


@router.get("/history")
async def queue_history(request: Request, user: dict = Depends(get_current_user)):
    """Get user's queue history."""
    db = request.app.state.db
    history = await db.get_user_queue_history(user["username"])
    return {"items": history}
