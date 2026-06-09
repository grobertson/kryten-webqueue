from fastapi import APIRouter, Request, Depends, HTTPException

from ..auth.session import require_admin
from ..catalog.db import HIDDEN_ITEM_TAG
from ..catalog.mediacms import MediaCMSClient

router = APIRouter(prefix="/admin/catalog", tags=["admin"])


def _mediacms_client(request: Request) -> MediaCMSClient:
    config = request.app.state.config
    return MediaCMSClient(mediacms_url=config.mediacms_url, token=config.mediacms_token)


@router.post("/{friendly_token}/hide")
async def hide_item(request: Request, friendly_token: str, user: dict = Depends(require_admin)):
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
    remote_ok = await _mediacms_client(request).set_tag(friendly_token, HIDDEN_ITEM_TAG, present=True)
    return {"success": True, "remote_ok": remote_ok}


@router.post("/{friendly_token}/unhide")
async def unhide_item(request: Request, friendly_token: str, user: dict = Depends(require_admin)):
    """Remove the ``kryten-hidden`` tag in MediaCMS and locally (B6)."""
    db = request.app.state.db
    item = await db.get_item_admin(friendly_token)
    if not item:
        raise HTTPException(404, "Catalog item not found")

    await db.remove_catalog_tag(friendly_token, HIDDEN_ITEM_TAG)
    remote_ok = await _mediacms_client(request).set_tag(friendly_token, HIDDEN_ITEM_TAG, present=False)
    return {"success": True, "remote_ok": remote_ok}
