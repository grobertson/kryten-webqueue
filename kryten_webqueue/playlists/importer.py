import logging
from datetime import datetime, UTC

logger = logging.getLogger(__name__)


class PlaylistImporter:
    """Imports items from a saved playlist into the live CyTube queue."""

    def __init__(self, *, api_gate, db, shadow):
        self._api_gate = api_gate
        self._db = db
        self._shadow = shadow

    async def import_playlist(self, playlist_id: int) -> dict:
        """Import all items from a saved playlist into the live queue."""
        items = await self._db.get_saved_playlist_items(playlist_id)
        if not items:
            return {"success": False, "error": "Playlist is empty"}

        added = 0
        errors = 0
        for item in items:
            try:
                result = await self._api_gate.playlist_add(
                    media_type=item["media_type"],
                    media_id=item["media_id"],
                    position="end",
                )
                if result.get("success"):
                    added += 1
                else:
                    errors += 1
            except Exception as e:
                logger.warning(f"Failed to add {item['media_id']}: {e}")
                errors += 1

        return {"success": True, "added": added, "errors": errors}


async def import_playlist_text(db, text: str) -> dict:
    """Parse plain-text playlist import format.

    Format:
      - Lines starting with # are comments
      - Blank lines are skipped
      - "type:id" for explicit type (e.g. "yt:dQw4w9WgXcQ")
      - "cm:friendly_token" for MediaCMS items
      - Bare token resolves from catalog

    Returns: {"items": [...], "errors": [...]}
    """
    items = []
    errors = []

    for line_num, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("cm:"):
            media_id = line[3:]
            catalog_item = await db.get_item_admin(media_id)
            items.append({
                "media_type": "cm",
                "media_id": media_id,
                "title": catalog_item["title"] if catalog_item else None,
                "duration_sec": catalog_item["duration_sec"] if catalog_item else None,
            })
        elif ":" in line:
            # Explicit type:id (e.g. yt:abc123)
            media_type, media_id = line.split(":", 1)
            items.append({
                "media_type": media_type,
                "media_id": media_id,
                "title": None,
                "duration_sec": None,
            })
        else:
            # Bare token — resolve from catalog
            catalog_item = await db.get_item_admin(line)
            if catalog_item:
                items.append({
                    "media_type": "cm",
                    "media_id": catalog_item["friendly_token"],
                    "title": catalog_item["title"],
                    "duration_sec": catalog_item["duration_sec"],
                })
            else:
                errors.append({"line": line_num, "token": line, "reason": "not_in_catalog"})

    return {"items": items, "errors": errors}
