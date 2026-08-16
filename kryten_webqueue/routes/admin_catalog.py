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


@router.post("/{friendly_token}/enrich")
async def enrich_item(
    request: Request, friendly_token: str, user: dict = Depends(require_admin)
):
    """Force-run the full enrichment pipeline for a single item.

    Starts as a background job and returns the run_id for progress polling.
    """
    db = request.app.state.db
    item = await db.get_item_admin(friendly_token)
    if not item:
        raise HTTPException(404, "Catalog item not found")
    job_manager = request.app.state.job_manager
    result = await job_manager.run(
        "catalog_enrich",
        params={
            "tokens": friendly_token,
            "force": "true",
            "steps": "classify,meta,art,tags",
        },
        triggered_by=user.get("username"),
    )
    return result


@router.patch("/{friendly_token}/edit")
async def edit_item(
    request: Request, friendly_token: str, user: dict = Depends(require_admin)
):
    """Manually edit catalog item metadata (core fields + enrichment state).

    Request body can include:
    - Core: title, description, duration_sec
    - Enrichment: content_type, lookup_title, lookup_year, hosted_show,
                  tv_show, tv_season, tv_episode_num
    - Options: sync_to_cms (bool), re_enrich (bool)

    All edits are logged to the audit trail. Returns summary of changes.
    """
    db = request.app.state.db
    item = await db.get_item_admin(friendly_token)
    if not item:
        raise HTTPException(404, "Catalog item not found")

    body = await request.json()
    username = user.get("username", "unknown")
    changes_logged = 0
    cms_synced = False
    enrich_job_id = None

    # --- Core catalog fields ---
    catalog_fields = {}
    for field in ["title", "description", "duration_sec"]:
        if field in body:
            new_value = body[field]
            old_value = item.get(field)
            if new_value != old_value:
                catalog_fields[field] = new_value
                await db.log_item_edit(
                    friendly_token, username, field, old_value, new_value
                )
                changes_logged += 1

    if catalog_fields:
        await db.update_catalog(friendly_token, catalog_fields)

    # --- Enrichment state fields ---
    enrichment_fields = {}
    # Get current enrichment state
    enrich_state = await db.get_enrichment_state(friendly_token) or {}

    for field in [
        "content_type",
        "lookup_title",
        "lookup_year",
        "hosted_show",
        "tv_show",
        "tv_season",
        "tv_episode_num",
    ]:
        if field in body:
            new_value = body[field]
            old_value = enrich_state.get(field)
            if new_value != old_value:
                enrichment_fields[field] = new_value
                await db.log_item_edit(
                    friendly_token, username, field, old_value, new_value
                )
                changes_logged += 1

    if enrichment_fields:
        await db.update_enrichment_state(friendly_token, enrichment_fields)

    # --- Optional MediaCMS sync ---
    if body.get("sync_to_cms", False) and (
        "title" in catalog_fields or "description" in catalog_fields
    ):
        cms = _mediacms_client(request)
        cms_synced = await cms.update_item(
            friendly_token,
            title=catalog_fields.get("title"),
            description=catalog_fields.get("description"),
        )

    # --- Optional re-enrichment trigger ---
    if body.get("re_enrich", False):
        job_manager = request.app.state.job_manager
        result = await job_manager.run(
            "catalog_enrich",
            params={
                "tokens": friendly_token,
                "force": "true",
                "steps": "classify,meta,art,tags",
            },
            triggered_by=username,
        )
        enrich_job_id = result.get("run_id")

    return {
        "success": True,
        "changes_logged": changes_logged,
        "cms_synced": cms_synced,
        "enrich_job_id": enrich_job_id,
    }


@router.get("/recently-played/debug")
async def recently_played_debug(request: Request, user: dict = Depends(require_admin)):
    """List what the recently-played rules currently hide from regular users."""
    db = request.app.state.db
    days = request.app.state.config.catalog_recently_played_hide_days
    return await db.get_recently_played_debug(days)


@router.delete("/{friendly_token}")
async def delete_catalog_item(
    request: Request, friendly_token: str, user: dict = Depends(require_admin)
):
    """
    Permanently delete a catalog item from SQLite and MediaCMS.

    HIGH-STAKES: This is a destructive operation with no recovery path.
    The item is removed from both the local catalog database and MediaCMS.

    Admin-only. Use with caution.
    """
    import logging

    logger = logging.getLogger(__name__)
    db = request.app.state.db

    # 1. Fetch item from catalog DB (need to verify it exists)
    item = await db.get_item_admin(friendly_token)
    if not item:
        raise HTTPException(404, "Item not found")

    # 2. Delete from MediaCMS
    cms_deleted = False
    cms_client = _mediacms_client(request)
    try:
        cms_deleted = await cms_client.delete_media(friendly_token)
        if not cms_deleted:
            logger.warning(
                f"MediaCMS deletion failed for {friendly_token}, continuing with local deletion"
            )
    except Exception as e:
        logger.error(
            f"MediaCMS deletion error for {friendly_token}: {e}, continuing with local deletion"
        )

    # 3. Delete from local catalog DB (source of truth for the webapp)
    deleted = await db.delete_catalog_item(friendly_token)

    if not deleted:
        raise HTTPException(500, "Failed to delete item from catalog")

    logger.info(
        f"Admin {user.get('username')} deleted catalog item {friendly_token} (title: {item.get('title')})"
    )

    return {
        "success": True,
        "deleted": friendly_token,
        "title": item.get("title"),
        "cms_deleted": cms_deleted,
    }
