from fastapi import APIRouter, Request, Depends, HTTPException

from ..auth.session import require_admin
from ..catalog.db import HIDDEN_ITEM_TAG
from ..catalog.mediacms import MediaCMSClient

router = APIRouter(prefix="/admin/catalog", tags=["admin"])


def _mediacms_client(request: Request) -> MediaCMSClient:
    config = request.app.state.config
    return MediaCMSClient(mediacms_url=config.mediacms_url, token=config.mediacms_token)


@router.post("/{friendly_token}/hide")
async def hide_item(
    request: Request, friendly_token: str, user: dict = Depends(require_admin)
):
    """Tag an item ``kryten-hidden`` in MediaCMS and hide it locally (B6).

    The local catalog is updated immediately so the admin sees the item
    disappear; the next sync re-reads the tag from MediaCMS (the source of
    truth) and the hide persists.
    """
    db = request.app.state.db
    item = await db.get_item_admin(friendly_token)
    if not item:
        raise HTTPException(404, "Catalog item not found")

    # Local mirror first so the UI hides immediately regardless of the remote
    # write outcome (which can be retried/confirmed by sync).
    await db.add_catalog_tag(friendly_token, HIDDEN_ITEM_TAG)
    remote_ok = await _mediacms_client(request).set_tag(
        friendly_token, HIDDEN_ITEM_TAG, present=True
    )
    return {"success": True, "remote_ok": remote_ok}


@router.post("/{friendly_token}/unhide")
async def unhide_item(
    request: Request, friendly_token: str, user: dict = Depends(require_admin)
):
    """Remove the ``kryten-hidden`` tag in MediaCMS and locally (B6)."""
    db = request.app.state.db
    item = await db.get_item_admin(friendly_token)
    if not item:
        raise HTTPException(404, "Catalog item not found")

    await db.remove_catalog_tag(friendly_token, HIDDEN_ITEM_TAG)
    remote_ok = await _mediacms_client(request).set_tag(
        friendly_token, HIDDEN_ITEM_TAG, present=False
    )
    return {"success": True, "remote_ok": remote_ok}


# --- Recently-played test helpers (admin-only) -----------------------------
#
# These let an admin exercise the v0.32 "hide recently-played" rules in situ
# without waiting for real playback: simulate a completion, clear an item's
# hide state, or inspect exactly what a regular user would not see.


@router.post("/{friendly_token}/mark-played")
async def mark_played(
    request: Request, friendly_token: str, user: dict = Depends(require_admin)
):
    """Simulate a genuine play-completion for an item (testing aid).

    Routes through the same ``record_play_completion`` the poll loop uses.
    The item then hides from regular users for the configured window, exactly
    as it would after real playback.
    """
    db = request.app.state.db
    item = await db.get_item_admin(friendly_token)
    if not item:
        raise HTTPException(404, "Catalog item not found")
    await db.record_play_completion(
        friendly_token=friendly_token,
        duration_sec=item.get("duration_sec"),
    )
    return {"success": True}


@router.post("/{friendly_token}/clear-played")
async def clear_played(
    request: Request, friendly_token: str, user: dict = Depends(require_admin)
):
    """Clear an item's recently-played hide state so it reappears immediately."""
    db = request.app.state.db
    item = await db.get_item_admin(friendly_token)
    if not item:
        raise HTTPException(404, "Catalog item not found")
    removed = await db.clear_play_state(friendly_token)
    return {"success": True, "removed": removed}


@router.get("/recently-played/debug")
async def recently_played_debug(request: Request, user: dict = Depends(require_admin)):
    """List what the recently-played rules currently hide from regular users."""
    db = request.app.state.db
    days = request.app.state.config.catalog_recently_played_hide_days
    return await db.get_recently_played_debug(days)
