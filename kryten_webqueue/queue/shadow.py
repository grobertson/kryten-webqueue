import asyncio
import logging
from datetime import datetime, timedelta, UTC

logger = logging.getLogger(__name__)


class QueueShadow:
    """Local mirror of the CyTube playlist state."""

    def __init__(self, db, now_playing: dict | None = None):
        self._db = db
        self._items: list[dict] = []
        self._now_playing: dict | None = now_playing
        self._lock = asyncio.Lock()

    @property
    def items(self) -> list[dict]:
        return self._items.copy()

    @property
    def now_playing(self) -> dict | None:
        return self._now_playing

    async def load_from_db(self):
        """Load initial state from database."""
        self._items = await self._db.get_shadow_items()

    async def apply_poll_result(self, playlist_items: list[dict], now_playing: dict | None):
        """Reconcile polled state with local shadow."""
        async with self._lock:
            self._now_playing = now_playing

            polled_uids = {item["uid"] for item in playlist_items}
            local_uids = {item["uid"] for item in self._items}

            # Removed items
            removed = local_uids - polled_uids
            if removed:
                await self._db.remove_shadow_items(removed)

            # Rebuild item list from poll result, preserving local metadata
            local_map = {item["uid"]: item for item in self._items}
            new_items = []

            for pos, polled in enumerate(playlist_items):
                uid = polled["uid"]
                if uid in local_map:
                    # Preserve local metadata, update position
                    merged = {**local_map[uid], **polled, "position": pos}
                else:
                    # New item from external source
                    merged = {
                        "uid": uid,
                        "position": pos,
                        "title": polled.get("title", ""),
                        "media_type": polled.get("type", "unknown"),
                        "media_id": polled.get("id", ""),
                        "duration_sec": float(polled.get("duration", 0) or 0),
                        "is_pay": False,
                        "paid_by": None,
                        "tier": None,
                        "z_cost": None,
                        "schedule_id": None,
                        "added_at": datetime.now(UTC).isoformat(),
                    }
                    await self._db.upsert_shadow_item(merged)

                new_items.append(merged)

            self._items = new_items
            await self._recalculate_estimated_starts()

    async def _recalculate_estimated_starts(self):
        """Recalculate estimated start times based on position and now-playing."""
        if not self._items:
            return

        # Start from now-playing elapsed or now
        start_cursor = datetime.now(UTC)
        if self._now_playing:
            remaining = float(self._now_playing.get("duration", 0) or 0) - float(self._now_playing.get("currentTime", 0) or 0)
            start_cursor += timedelta(seconds=max(0, remaining))

        for item in self._items:
            item["estimated_start_at"] = start_cursor.isoformat()
            duration = float(item.get("duration_sec", 0) or 0)
            start_cursor += timedelta(seconds=duration)

    async def insert_at(self, item: dict, position: int):
        """Insert a new item at given position in local shadow."""
        async with self._lock:
            item["position"] = position
            self._items.insert(position, item)
            # Re-index
            for i, it in enumerate(self._items):
                it["position"] = i
            await self._db.upsert_shadow_item(item)
            await self._recalculate_estimated_starts()

    async def remove(self, uid: int):
        """Remove item from shadow."""
        async with self._lock:
            self._items = [it for it in self._items if it["uid"] != uid]
            for i, it in enumerate(self._items):
                it["position"] = i
            await self._db.remove_shadow_items({uid})
            await self._recalculate_estimated_starts()

    def get_queue_state(self) -> dict:
        """Return serializable queue state for WebSocket broadcast."""
        return {
            "items": self._items,
            "now_playing": self._now_playing,
            "updated_at": datetime.now(UTC).isoformat(),
        }
