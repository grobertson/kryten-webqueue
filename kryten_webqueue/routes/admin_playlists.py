from fastapi import APIRouter, Request, Depends, HTTPException
from datetime import datetime

from ..auth.session import require_admin

router = APIRouter(prefix="/admin/playlists", tags=["admin"])


@router.get("/")
async def list_playlists(request: Request, user: dict = Depends(require_admin)):
    """List all saved playlists."""
    db = request.app.state.db
    return await db.get_saved_playlists()


@router.get("/{playlist_id}")
async def get_playlist(request: Request, playlist_id: int, user: dict = Depends(require_admin)):
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
    )
    return {"id": playlist_id}


@router.put("/{playlist_id}")
async def update_playlist(request: Request, playlist_id: int, user: dict = Depends(require_admin)):
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
    )
    return {"success": True}


@router.delete("/{playlist_id}")
async def delete_playlist(request: Request, playlist_id: int, user: dict = Depends(require_admin)):
    """Delete a saved playlist."""
    db = request.app.state.db
    playlist = await db.get_saved_playlist(playlist_id)
    if not playlist:
        raise HTTPException(404, "Playlist not found")
    await db.delete_saved_playlist(playlist_id)
    return {"success": True}


@router.put("/{playlist_id}/items")
async def replace_items(request: Request, playlist_id: int, user: dict = Depends(require_admin)):
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
async def import_to_live(request: Request, playlist_id: int, user: dict = Depends(require_admin)):
    """Import a saved playlist into the live CyTube queue."""
    from ..playlists.importer import PlaylistImporter

    db = request.app.state.db
    playlist = await db.get_saved_playlist(playlist_id)
    if not playlist:
        raise HTTPException(404, "Playlist not found")

    importer = PlaylistImporter(
        api_gate=request.app.state.api_gate,
        db=db,
        shadow=request.app.state.shadow,
    )
    result = await importer.import_playlist(playlist_id)
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
    count = await db.append_playlist_item(playlist["id"], {
        "media_type": "cm",
        "media_id": item["manifest_url"],
        "title": item.get("title"),
        "duration_sec": item.get("duration_sec"),
    })
    return {"success": True, "playlist_id": playlist["id"], "name": playlist["name"], "count": count}


@router.post("/{playlist_id}/append")
async def append_item(request: Request, playlist_id: int, user: dict = Depends(require_admin)):
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
    count = await db.append_playlist_item(playlist_id, {
        "media_type": "cm",
        "media_id": item["manifest_url"],
        "title": item.get("title"),
        "duration_sec": item.get("duration_sec"),
    })
    return {"success": True, "playlist_id": playlist_id, "name": playlist["name"], "count": count}


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
