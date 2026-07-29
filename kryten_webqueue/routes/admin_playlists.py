from fastapi import APIRouter, Request, Depends, HTTPException
from datetime import datetime

from ..auth.session import require_admin
from ..promos import PROMO_TYPES

router = APIRouter(prefix="/admin/playlists", tags=["admin"])


def _validate_promo_type(value):
    """Normalise/validate a promo_type from a request body.

    Accepts ``None``/empty (a normal playlist) or one of the recognised promo
    types; rejects anything else with HTTP 400.
    """
    if value in (None, "", "none"):
        return None
    if value not in PROMO_TYPES:
        raise HTTPException(400, f"Invalid promo_type: {value!r}")
    return value


@router.get("/")
async def list_playlists(request: Request, user: dict = Depends(require_admin)):
    """List all saved playlists."""
    db = request.app.state.db
    return await db.get_saved_playlists()


@router.get("/{playlist_id}")
async def get_playlist(
    request: Request, playlist_id: int, user: dict = Depends(require_admin)
):
    """Get a saved playlist with its items."""
    db = request.app.state.db
    playlist = await db.get_saved_playlist(playlist_id)
    if not playlist:
        raise HTTPException(404, "Playlist not found")
    items = await db.get_saved_playlist_items(playlist_id)
    return {**playlist, "items": items}


@router.post("/")
async def create_playlist(request: Request, user: dict = Depends(require_admin)):
    """Create a new saved playlist."""
    body = await request.json()
    db = request.app.state.db
    playlist_id = await db.create_saved_playlist(
        name=body["name"],
        description=body.get("description"),
        is_immutable=body.get("is_immutable", False),
        created_by=user["username"],
        promo_type=_validate_promo_type(body.get("promo_type")),
    )
    return {"id": playlist_id}


@router.put("/{playlist_id}")
async def update_playlist(
    request: Request, playlist_id: int, user: dict = Depends(require_admin)
):
    """Update playlist metadata."""
    body = await request.json()
    db = request.app.state.db
    playlist = await db.get_saved_playlist(playlist_id)
    if not playlist:
        raise HTTPException(404, "Playlist not found")
    await db.update_saved_playlist(
        playlist_id,
        name=body.get("name", playlist["name"]),
        description=body.get("description", playlist.get("description")),
        is_immutable=body.get("is_immutable", playlist.get("is_immutable", False)),
        promo_type=_validate_promo_type(
            body.get("promo_type", playlist.get("promo_type"))
        ),
    )
    return {"success": True}


@router.delete("/{playlist_id}")
async def delete_playlist(
    request: Request, playlist_id: int, user: dict = Depends(require_admin)
):
    """Delete a saved playlist."""
    db = request.app.state.db
    playlist = await db.get_saved_playlist(playlist_id)
    if not playlist:
        raise HTTPException(404, "Playlist not found")
    await db.delete_saved_playlist(playlist_id)
    return {"success": True}


@router.put("/{playlist_id}/items")
async def replace_items(
    request: Request, playlist_id: int, user: dict = Depends(require_admin)
):
    """Replace all items in a playlist."""
    body = await request.json()
    items = body.get("items", [])
    db = request.app.state.db
    playlist = await db.get_saved_playlist(playlist_id)
    if not playlist:
        raise HTTPException(404, "Playlist not found")
    await db.replace_playlist_items(playlist_id, items)
    return {"success": True, "count": len(items)}


@router.post("/{playlist_id}/import")
async def import_to_live(
    request: Request,
    playlist_id: int,
    full: int = 0,
    user: dict = Depends(require_admin),
):
    """Import a saved playlist into the live CyTube queue.

    By default this honors the current-pass played/skip rules (a mutable TV-show
    playlist continues where it left off). Pass ``?full=1`` to force-load the
    entire list regardless of what's already been played this pass.
    """
    from ..playlists.importer import PlaylistImporter

    db = request.app.state.db
    playlist = await db.get_saved_playlist(playlist_id)
    if not playlist:
        raise HTTPException(404, "Playlist not found")

    config = request.app.state.config
    importer = PlaylistImporter(
        api_gate=request.app.state.api_gate,
        db=db,
        shadow=request.app.state.shadow,
        add_delay_sec=config.playlist_bulk_add_delay_sec,
        add_max_retries=config.playlist_bulk_add_max_retries,
        promo_director=getattr(request.app.state, "promo_director", None),
    )
    result = await importer.import_playlist(playlist_id, skip_played=not bool(full))
    return result


@router.post("/recent/append")
async def append_item_recent(request: Request, user: dict = Depends(require_admin)):
    """Append a catalog item to the admin's most-recently-created playlist (B5).

    Registered before /{playlist_id}/append so the literal path wins.
    """
    body = await request.json()
    token = body.get("friendly_token")
    if not token:
        raise HTTPException(400, "friendly_token required")
    db = request.app.state.db
    playlist = await db.get_most_recent_playlist(user["username"])
    if not playlist:
        raise HTTPException(409, "Create a playlist first")
    item = await db.get_item_admin(token)
    if not item:
        raise HTTPException(404, "Catalog item not found")
    count = await db.append_playlist_item(
        playlist["id"],
        {
            "media_type": "cm",
            "media_id": item["manifest_url"],
            "title": item.get("title"),
            "duration_sec": item.get("duration_sec"),
        },
    )
    return {
        "success": True,
        "playlist_id": playlist["id"],
        "name": playlist["name"],
        "count": count,
    }


@router.post("/{playlist_id}/append")
async def append_item(
    request: Request, playlist_id: int, user: dict = Depends(require_admin)
):
    """Append a single catalog item to a playlist (B4)."""
    body = await request.json()
    token = body.get("friendly_token")
    if not token:
        raise HTTPException(400, "friendly_token required")
    db = request.app.state.db
    playlist = await db.get_saved_playlist(playlist_id)
    if not playlist:
        raise HTTPException(404, "Playlist not found")
    item = await db.get_item_admin(token)
    if not item:
        raise HTTPException(404, "Catalog item not found")
    count = await db.append_playlist_item(
        playlist_id,
        {
            "media_type": "cm",
            "media_id": item["manifest_url"],
            "title": item.get("title"),
            "duration_sec": item.get("duration_sec"),
        },
    )
    return {
        "success": True,
        "playlist_id": playlist_id,
        "name": playlist["name"],
        "count": count,
    }


@router.post("/{playlist_id}/append-results")
async def append_results(
    request: Request, playlist_id: int, user: dict = Depends(require_admin)
):
    """Append every catalog item matching the current browse/search filters to a
    playlist (0.14.2).

    The browse/search facets are sent in the body so the server re-runs the same
    (unpaginated) query the admin is looking at. Items are laid out in
    season/episode order where a marker is detectable in the title, and any item
    already present in the playlist is skipped.
    """
    from ..playlists.ordering import order_for_playlist

    body = await request.json()
    db = request.app.state.db
    playlist = await db.get_saved_playlist(playlist_id)
    if not playlist:
        raise HTTPException(404, "Playlist not found")

    mode = body.get("mode", "browse")
    show_hidden = bool(body.get("show_hidden"))
    sort = body.get("sort") or "default"

    if mode == "search":
        q = (body.get("q") or "").strip()
        if not q:
            raise HTTPException(400, "Query required for search results")
        total = await db.search_count(q, show_hidden=show_hidden)
        items = await db.search(
            q, page=1, per_page=max(total, 1), show_hidden=show_hidden, sort=sort
        )
    else:
        category = body.get("category") or None
        tag = body.get("tag") or None
        total = await db.browse_count(
            category=category, tag=tag, show_hidden=show_hidden
        )
        items = await db.browse(
            category=category,
            tag=tag,
            page=1,
            per_page=max(total, 1),
            show_hidden=show_hidden,
            sort=sort,
        )

    ordered = order_for_playlist(items)
    playlist_items = [
        {
            "media_type": "cm",
            "media_id": it["manifest_url"],
            "title": it.get("title"),
            "duration_sec": it.get("duration_sec"),
        }
        for it in ordered
        if it.get("manifest_url")
    ]
    added = await db.append_playlist_items(playlist_id, playlist_items)
    count = len(await db.get_saved_playlist_items(playlist_id))
    return {
        "success": True,
        "playlist_id": playlist_id,
        "name": playlist["name"],
        "added": added,
        "count": count,
    }


@router.post("/parse-text")
async def parse_text(request: Request, user: dict = Depends(require_admin)):
    """Parse the plain-text playlist import format into resolved items.

    Stateless: returns {items, errors} for the editor to merge into its working
    list. Persistence happens via PUT /{id}/items when the admin saves.
    """
    from ..playlists.importer import import_playlist_text

    body = await request.json()
    text = body.get("text", "")
    db = request.app.state.db
    config = request.app.state.config
    return await import_playlist_text(db, text, mediacms_url=config.mediacms_url)
