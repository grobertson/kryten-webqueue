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
